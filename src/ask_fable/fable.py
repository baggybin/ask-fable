"""Invoke the Fable model for a single reasoning turn.

The model id is NOT pinned: ``fable_model()`` walks ``FABLE_CANDIDATES``
newest-first (5.1, then 5) and returns the best one this process has not seen a
transport reject, so `ask` tracks the newest Fable instead of whichever id was
current when this line was written. ``ASK_FABLE_FABLE_MODEL`` pins an exact id.

Primary path is the Claude Agent SDK in-process, mirroring salient-core's
``daemon/_backend.py`` ``LocalClaudeBackend``: it reuses Claude Code's existing
OAuth session (``~/.claude/.credentials.json``) — we deliberately do NOT set
``ANTHROPIC_API_KEY``. Tools are disabled so this is a pure reasoning oracle.

Multi-turn: pass ``resume=<session_id>`` (captured from a prior turn's
``ResultMessage.session_id``) to continue a conversation — Fable keeps context
server-side, so we never re-send the transcript.

Fallback path shells out to the ``claude`` CLI in print mode (same OAuth), for
environments without the SDK. It resumes too: its JSON output carries the session
id, and a follow-up hands it back as ``--resume``.

This module is also the shared Anthropic bridge: every Claude model reachable
over the same OAuth session runs through it, selected by a ``ClaudeSpec``.
``opus.py`` is a thin wrapper that passes ``OPUS`` instead of the ``FABLE``
default — same transport, same prompts, same telemetry shape, different model.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from . import anthropic_http, cli_gate
from .isolation import oracle_cwd
from .oracle_common import (
    OracleResult,
    cli_error_detail,
    compose,
    http_error_detail,
    is_provider_refusal,
    shape_stopped,
    timeout_default,
    websearch_max_turns,
)
from .prompts import FABLE_SYSTEM_PROMPT
from .provider_telemetry import (
    ProviderTelemetry,
    ProviderUsage,
    normalize_cost_usd,
    token_count,
    usage_if_available,
)
from .redaction import redact_text

FABLE_MODEL = "claude-fable-5"  # the floor: always served, always accepted
FABLE_PREFERRED_MODEL = "claude-fable-5-1"

# Fable ids newest-first. `fable_model()` returns the first one this process has
# not proved unusable, so `ask` tracks the best Fable available instead of being
# pinned to whichever id was current when the code was written. A transport that
# rejects the preferred id (an older Claude Code build, say) demotes it once and
# the turn is retried one rung down, so a stale environment degrades to Fable 5
# rather than failing.
FABLE_CANDIDATES: tuple[str, ...] = (FABLE_PREFERRED_MODEL, FABLE_MODEL)

MODEL_ENV = "ASK_FABLE_FABLE_MODEL"  # pin an exact id, skipping the ladder
CLI_ENV = "ASK_FABLE_CLAUDE_CLI"  # pin the Claude Code binary the SDK spawns

# Where a turn is allowed to run. `auto` walks the Claude Code transports only; the
# API path is opt-in by name, because falling through to it on the strength of an
# exported key would turn a flat-plan oracle into a per-token one without anyone
# asking — the exact surprise this selector exists to prevent.
TRANSPORT_ENV = "ASK_FABLE_FABLE_TRANSPORT"  # auto | sdk | cli | http
LEGACY_CLI_ENV = "ASK_FABLE_USE_CLI"  # superseded by TRANSPORT_ENV; folded in as cli/sdk
TRANSPORTS: tuple[str, ...] = ("auto", "sdk", "cli", "http")

# The only failure kinds `auto` may fall past: the transport is not THERE. Every
# other kind means a transport answered — a refusal, a timeout, a 429, a rejected
# model, an empty answer — and re-routing on one of those would answer the same
# question over a different path (and possibly a different bill) with no signal to
# the caller. `transport_incapable` is deliberately absent: re-routing on it would
# ping-pong between transports that each cannot do the job.
REROUTE_KINDS: frozenset[str] = frozenset({"binary_missing", "sdk_unavailable"})

# The http transport keeps no server-side conversation, so it has no id to hand
# back for a resume. A turn over it returns this marker as its session id instead,
# so the session still RECORDS that a conversation exists: the follow-up carries
# the marker as `resume` and is refused `transport_incapable` on every transport.
# Without it the follow-up carried no resume at all and was answered as a fresh,
# memoryless thread that still reported ok.
HTTP_SESSION_PREFIX = "http:"

# The Anthropic Messages API transport (no Claude Code required).
HTTP_KEY_ENV = "ASK_FABLE_ANTHROPIC_API_KEY"
HTTP_BASE_URL_ENV = "ASK_FABLE_ANTHROPIC_BASE_URL"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")

# Model ids this process has seen a transport reject, keyed (transport, model).
# Process-scoped on purpose: it is a fact about the local Claude Code build, so it
# must not outlive an upgrade. Keyed by transport because the id namespaces differ —
# a Messages-API rejection says nothing about what the OAuth ladder serves, and a
# shared set would poison the model everywhere for the rest of the process.
_unavailable: set[tuple[str | None, str]] = set()

_ALL_TRANSPORTS: tuple[str, ...] = ("sdk", "cli", "http")

# A reasoning turn must send NOTHING derived from the caller's repo, and must not pay
# for Claude Code's built-in tool schemas or bundled skills either. Four layers:
#   1. spawn in a controlled, empty cwd (`isolation.oracle_cwd`) — nothing to discover;
#   2. `--safe-mode` — the CLI's "no customizations" switch (CLAUDE.md, AGENTS.md, skills,
#      plugins, hooks, MCP, agents). Keeps OAuth, unlike `--bare`, which forces an API key
#      and would break the flat-plan session this bridge deliberately reuses;
#   3. the env opt-outs below (auto-memory, which `--safe-mode` does not list, and the
#      bundled/policy skill listings it does not suppress);
#   4. `tools=[]` in the SDK options (epilogue: `--tools ""`). Without it the SDK sent the
#      FULL built-in tool schema in the system prompt (~109 KB: EnterWorktree, Read, Bash,
#      …) even though `allowed_tools=[]` blocked their use — the CLI path always passed
#      `--tools ""`, the SDK path never did.
_ISOLATION_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_POLICY_SKILLS": "1",
}
_ISOLATION_EXTRA_ARGS: dict[str, str | None] = {"safe-mode": None}


@dataclass(frozen=True)
class ClaudeSpec:
    """Which Anthropic model a bridge call runs, and how it is named.

    ``model`` is the API/CLI model id, ``key`` the oracle-registry key (rides on
    telemetry so traces attribute correctly), and ``label`` the human name used
    in error text and console output."""

    model: str
    key: str
    label: str


# The explicit `fable51` oracle token — pinned to 5.1 and never laddered, so it
# still means 5.1 once the ladder has moved on to a later release. There is no
# matching `FABLE` constant on purpose: `fable` IS the ladder, and a spec that
# looked like the default while quietly pinning the floor is a trap.
FABLE51 = ClaudeSpec(model=FABLE_PREFERRED_MODEL, key="fable51", label="Fable 5.1")


@dataclass(frozen=True)
class Ladder:
    """One Anthropic model FAMILY resolved newest-first at call time.

    This is the machinery behind a version-neutral token like ``fable`` or
    ``opus``: ``candidates`` is the id list newest-first (its last entry is the
    floor — always served, always accepted), ``model_env`` names the exact-id pin
    env var that skips the ladder, and ``key``/``label`` are the oracle-registry
    key and human name carried on every rung (the label stays generic — "Fable",
    "Opus" — because *which* id answered is already reported by
    ``OracleResult.model``). ``opus.OPUS_LADDER`` is the second instance; add a
    family by constructing a third, not by copying this logic."""

    key: str
    label: str
    candidates: tuple[str, ...]
    model_env: str


# The `fable` family. `opus.py` builds the matching `opus` ladder; both flow
# through the generic resolvers below and through ``run(ladder=...)``.
FABLE_LADDER = Ladder(key="fable", label="Fable", candidates=FABLE_CANDIDATES, model_env=MODEL_ENV)


def ladder_pin(ladder: Ladder) -> str:
    """The operator's exact-id pin for ``ladder`` (its ``model_env``), or ""."""
    return (os.environ.get(ladder.model_env) or "").strip()


def ladder_model(ladder: Ladder, transport: str | None = None) -> str:
    """The model id this process will actually ask for on ``ladder``.

    A pin wins outright; otherwise the newest candidate this process has not proved
    unusable. Read at call time so an operator can pin (or unpin) without
    restarting the server.

    ``transport`` scopes the question: "unusable *here*" for a call about to run on
    that transport, or "unusable anywhere" (the default, which is what the display
    callers want) when no transport is named."""
    pin = ladder_pin(ladder)
    if pin:
        return pin
    demoted = (
        (lambda candidate: any((t, candidate) in _unavailable for t in _ALL_TRANSPORTS))
        if transport is None
        else (lambda candidate: (transport, candidate) in _unavailable)
    )
    for candidate in ladder.candidates:
        if not demoted(candidate):
            return candidate
    return ladder.candidates[-1]


def ladder_spec(ladder: Ladder, transport: str | None = None) -> ClaudeSpec:
    """The ``ladder`` spec with its model resolved through the rungs. The label
    stays generic at every rung — which id answered is already reported by
    ``OracleResult.model`` and ``ProviderTelemetry.actual_model``."""
    return ClaudeSpec(model=ladder_model(ladder, transport), key=ladder.key, label=ladder.label)


def pinned_model() -> str:
    """The operator's exact-id pin from ``ASK_FABLE_FABLE_MODEL``, or ""."""
    return ladder_pin(FABLE_LADDER)


def fable_model(transport: str | None = None) -> str:
    """The Fable model id this process will actually ask for (the `fable` ladder)."""
    return ladder_model(FABLE_LADDER, transport)


def fable_spec(transport: str | None = None) -> ClaudeSpec:
    """The `fable` spec with its model resolved through the ladder."""
    return ladder_spec(FABLE_LADDER, transport)


def _demote(candidates: tuple[str, ...], transport: str, model: str) -> str | None:
    """Record that ``transport`` rejected ``model``; return the next id on
    ``candidates`` this transport has not rejected, or None when the floor is
    exhausted."""
    _unavailable.add((transport, model))
    for candidate in candidates:
        if (transport, candidate) not in _unavailable:
            return candidate
    return None


class TransportConfigError(ValueError):
    """A bad ``ASK_FABLE_FABLE_TRANSPORT`` value — a config error, never a fallback."""


@dataclass(frozen=True, slots=True)
class TransportPlan:
    """Where one call may run, resolved ONCE per ``run`` and then executed.

    Resolved in ``run`` rather than in ``_dispatch`` because ``_dispatch`` runs
    twice per call — the model ladder retries through it — so a plan re-resolved
    there could answer attempt 1 over one transport and attempt 2 over another, and
    the chosen transport would be invisible to the frame that reports it.

    ``attempts`` is the ordered transport list; a pinned selector yields exactly one
    entry (no fallback). ``warning`` surfaces a legacy/new selector disagreement."""

    attempts: tuple[str, ...]
    warning: str | None = None


def http_api_key() -> str:
    """The operator's Anthropic API key, or "" (the http transport's only config)."""
    return (os.environ.get(HTTP_KEY_ENV) or "").strip()


def http_base_url() -> str:
    return (os.environ.get(HTTP_BASE_URL_ENV) or "").strip() or ANTHROPIC_BASE_URL


def claude_code_present() -> bool:
    """True when a Claude Code transport EXISTS: the SDK is importable or the
    ``claude`` CLI is on PATH.

    Presence, not health — an installed-but-unauthenticated CLI reports present here
    and its auth error then propagates rather than re-routing. That is deliberate:
    adding auth errors to the re-route set would let a transient hiccup escalate a
    turn onto a billed transport."""
    if shutil.which("claude"):
        return True
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def http_transport_selected() -> bool:
    """True when the operator has explicitly pinned the API transport.

    Never raises: a bad selector is reported by ``transport_plan`` at call time, and
    a health probe must not blow up on a typo it is supposed to help diagnose."""
    try:
        return transport_plan().attempts == ("http",)
    except TransportConfigError:
        return False


def transport_plan() -> TransportPlan:
    """Resolve the transport attempts for one call.

    Raises :class:`TransportConfigError` on an unrecognised selector — a typo must
    never quietly answer over a path the operator did not choose."""
    raw = (os.environ.get(TRANSPORT_ENV) or "").strip().lower()
    if raw and raw not in TRANSPORTS:
        raise TransportConfigError(
            f"{TRANSPORT_ENV}={raw!r} is not a transport; choose one of {list(TRANSPORTS)}"
        )
    legacy = (os.environ.get(LEGACY_CLI_ENV) or "").strip().lower()
    legacy_pick = "cli" if legacy in _TRUTHY else "sdk" if legacy in _FALSY else None
    warning: str | None = None
    if raw and raw != "auto":
        selector = raw
        if legacy_pick and legacy_pick != selector:
            warning = (
                f"{TRANSPORT_ENV}={selector} won over {LEGACY_CLI_ENV}={legacy_pick}; "
                f"unset {LEGACY_CLI_ENV} to silence this"
            )
    elif legacy_pick:
        selector = legacy_pick
    else:
        selector = "auto"
    # `auto` is the Claude Code ladder only — see TRANSPORTS above for why the API
    # path is not on it.
    attempts = ("sdk", "cli") if selector == "auto" else (selector,)
    return TransportPlan(attempts=attempts, warning=warning)


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _cli_version(path: str) -> tuple[int, ...] | None:
    """`<path> --version` parsed into a comparable tuple, or None if it won't run."""
    try:
        out = subprocess.run(  # noqa: S603 — a Claude Code binary we resolved ourselves
            [path, "--version"],
            capture_output=True,
            text=True,
            # `errors="replace"`, and ValueError caught below, because a binary that
            # prints non-UTF-8 on --version raised UnicodeDecodeError out of here.
            # This runs inside `best_cli_path()`, which `_run_sdk` calls BEFORE its
            # try block, and `run()` has no catch-all — so the exception escaped
            # `fable.run` entirely: no audit row, no breaker record, and the `auto`
            # ladder never fell through to the CLI. (`diagnose` already fixed its own
            # copy of this probe; this is the sibling it left behind.)
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    m = _VERSION_RE.search(f"{out.stdout or ''}{out.stderr or ''}")
    return tuple(int(g) for g in m.groups()) if m else None


def _bundled_cli() -> str | None:
    """The Claude Code binary vendored inside claude-agent-sdk, if present."""
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    name = "claude.exe" if os.name == "nt" else "claude"
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return str(path) if path.is_file() else None


@functools.cache
def best_cli_path() -> str | None:
    """Which Claude Code binary the SDK should spawn — or None to let it choose.

    The Agent SDK prefers its own vendored binary over the one on PATH, and that
    copy only moves when the SDK is upgraded. A model newer than it is rejected
    with "does not support this model", which is exactly how Fable 5.1 fails on
    an SDK whose bundle predates it while the operator's own `claude` runs it
    fine. So: hand the SDK the PATH binary when it is strictly newer, leave its
    choice alone otherwise, and let ``ASK_FABLE_CLAUDE_CLI`` override both.
    Cached — the two `--version` probes are worth paying once per process."""
    override = (os.environ.get(CLI_ENV) or "").strip()
    if override:
        return override
    system = shutil.which("claude")
    if not system:
        return None  # nothing to offer; the SDK's bundle is the only candidate
    bundled = _bundled_cli()
    if bundled is None:
        return system
    system_v = _cli_version(system)
    if system_v is None:
        return None
    bundled_v = _cli_version(bundled)
    return system if bundled_v is None or system_v > bundled_v else None


async def run(
    question: str,
    context: str = "",
    *,
    resume: str | None = None,
    timeout: float | None = None,
    use_cli: bool | None = None,
    system_prompt: str | None = None,
    on_think: Callable[[str], None] | None = None,
    spec: ClaudeSpec | None = None,
    ladder: Ladder | None = None,
    web_search: bool = False,
) -> OracleResult:
    """Run one Fable turn. Returns an OracleResult (never raises for expected
    failures). ``resume`` continues a prior Claude Code session on the SDK or CLI
    transport; the http transport refuses it (``transport_incapable``). A resumed
    turn that failed because the id itself is dead sets ``meta["resume_failed"]``.
    ``system_prompt`` overrides the default oracle prompt (used for synthesis).
    ``web_search`` (opt-in, used by ``ask_websearch``) allows the native
    ``WebSearch``/``WebFetch`` tools over the OAuth session and raises ``max_turns``
    so the model can search then answer — otherwise this is a toolless turn.
    ``on_think`` is a best-effort sink called with each reasoning block as it streams
    from the SDK (for a live console trace); ignored by the CLI path.
    ``spec`` selects a PINNED Anthropic model (``fable51.run`` / the
    ``anthropic_variants`` pins pass one). ``ladder`` selects a version-neutral
    FAMILY resolved newest-first — omit both for the `fable` family (the default),
    or pass ``ladder=opus.OPUS_LADDER`` for `opus`. A ``spec`` wins over ``ladder``;
    with neither, the `fable` ladder is used.

    Where the turn may run comes from ``ASK_FABLE_FABLE_TRANSPORT``
    (``transport_plan``), resolved once here. Under ``auto`` a transport that is
    simply absent (no SDK, no ``claude`` on PATH) is stepped past; a pinned
    transport never falls back.

    Only a laddered call falls back on the MODEL: if the transport rejects the id
    outright (``model_unavailable`` — an older Claude Code build, typically), that
    id is demoted for the life of the process *on that transport* and the turn is
    retried one rung down. An explicit spec or an ``ASK_FABLE_FABLE_MODEL`` pin is
    honored as written — silently answering as a different model than the operator
    named would make an A/B meaningless."""
    timeout = timeout if timeout is not None else timeout_default()
    system_prompt = system_prompt or FABLE_SYSTEM_PROMPT
    active = ladder if ladder is not None else FABLE_LADDER
    try:
        plan = transport_plan()
        if use_cli is not None:
            # The in-process API for one call (fable51/opus/tests) still wins over
            # the env, but it is a single-attempt plan like any other pin.
            plan = TransportPlan(attempts=(("cli" if use_cli else "sdk"),), warning=plan.warning)
    except TransportConfigError as exc:
        return OracleResult(
            "error", kind="bad_args", text=str(exc),
            model=spec.model if spec is not None else ladder_pin(active) or "",
        )
    laddered = spec is None and not ladder_pin(active)
    message = compose(question, context)
    result: OracleResult | None = None
    for transport in plan.attempts:
        attempt_spec = spec if spec is not None else ladder_spec(active, transport)
        result = await _dispatch(
            transport, message, question, context, timeout, resume, system_prompt,
            on_think, attempt_spec, web_search,
        )
        if laddered and result.kind == "model_unavailable":
            nxt = _demote(active.candidates, transport, attempt_spec.model)
            if nxt is not None:
                # `resume` is dropped: an SDK session id is bound to the model that
                # created it (the same reason `ask_opus5` namespaces its sessions),
                # so the fallback starts a fresh thread rather than resuming another
                # model's conversation.
                result = await _dispatch(
                    transport, message, question, context, timeout, None, system_prompt,
                    on_think, replace(attempt_spec, model=nxt), web_search,
                )
        if result.kind not in REROUTE_KINDS:
            break
    assert result is not None  # TRANSPORTS is non-empty; a plan always has an attempt
    if plan.warning:
        result.meta.setdefault("transport_warning", plan.warning)
    return result


async def _dispatch(
    transport: str,
    message: str,
    question: str,
    context: str,
    timeout: float,
    resume: str | None,
    system_prompt: str,
    on_think: Callable[[str], None] | None,
    spec: ClaudeSpec,
    web_search: bool = False,
) -> OracleResult:
    """One turn on one model over ONE named transport.

    The fallback that used to live here (``except ImportError: _run_cli``) is gone
    on purpose: it fired whichever transport the operator had asked for, so a
    pinned ``sdk`` whose lazy import failed would quietly spawn the CLI instead.
    The absence is reported as ``sdk_unavailable`` and it is the caller's attempt
    loop — which only ever steps past :data:`REROUTE_KINDS`, and only when the plan
    has another rung — that decides whether to try something else."""
    if resume and resume.startswith(HTTP_SESSION_PREFIX):
        return OracleResult(
            "error", kind="transport_incapable", model=spec.model,
            text=(
                "this session's earlier turn ran over the http transport, which keeps no "
                "server-side conversation, so no transport can continue it; reset the "
                "session (reset=true) or start a new one"
            ),
        )
    if transport == "cli":
        return await _run_cli(message, timeout, system_prompt, spec, web_search, resume=resume)
    if transport == "http":
        return await _run_http(question, context, timeout, system_prompt, spec, resume, web_search)
    try:
        return await _run_sdk(message, timeout, resume, system_prompt, on_think, spec, web_search)
    except ImportError:
        return OracleResult(
            "error", kind="sdk_unavailable", model=spec.model,
            text=(
                "the Claude Agent SDK is not importable, so the in-process transport "
                "is unavailable (install claude-agent-sdk, or set "
                f"{TRANSPORT_ENV}=cli to use the `claude` CLI)"
            ),
        )


async def _run_http(
    question: str,
    context: str,
    timeout: float,
    system_prompt: str,
    spec: ClaudeSpec,
    resume: str | None,
    web_search: bool,
) -> OracleResult:
    """One turn over the Anthropic Messages API with the operator's own key — the
    transport for a host with no Claude Code.

    ``resume`` is refused rather than degraded: there is no server-side session to
    continue over HTTP, and answering a follow-up as a fresh thread would silently
    change what the caller asked for. An answered turn carries an
    :data:`HTTP_SESSION_PREFIX` marker as its session id, so a follow-up on the same
    session really does arrive with a ``resume`` to refuse. ``web_search`` IS
    supported here — via the API's own server-side search tool, which needs no client
    loop and no Claude Code (`anthropic_http.WEB_SEARCH_TOOL`), so an
    ``ask_websearch`` turn works on a host with no CLI at all."""
    if resume:
        return OracleResult(
            "error", kind="transport_incapable", model=spec.model,
            text=(
                "the http transport cannot resume a session (Claude Code SDK sessions "
                f"are server-side); start a new thread, or set {TRANSPORT_ENV}=sdk"
            ),
        )
    key = http_api_key()
    if not key:
        return OracleResult(
            "error", kind="not_configured", model=spec.model,
            text=f"the http transport needs {HTTP_KEY_ENV} set (and optionally {HTTP_BASE_URL_ENV})",
        )
    cfg = anthropic_http.ProviderConfig(
        key=spec.key, label=spec.label, model=spec.model,
        base_url=http_base_url(), api_key=key,
    )
    result = await anthropic_http.run(
        cfg, question, context, timeout=timeout, system_prompt=system_prompt,
        web_search=web_search,
    )
    # The OAuth paths declare `cost_basis="subscription"` (flat plan, marginal cost
    # zero). This one is real per-token spend, so it must never be confused for it —
    # and it must not invent a `cost_usd` either (there is no price table here, and
    # `cost_basis` only rides next to a cost in the telemetry schema). So the basis
    # travels as a caller-visible fact instead of a fabricated number.
    result.meta.setdefault("cost_basis", "billed")
    if result.status == "ok":
        result.session_id = f"{HTTP_SESSION_PREFIX}{uuid.uuid4()}"
    return result


_MAX_TURNS_MARKERS = ("max_turns", "max turns", "maximum turns")


def _is_max_turns_error(err: object) -> bool:
    """True when the SDK error names the agentic turn-budget wall (subtype
    ``error_max_turns``), so the driver can salvage partial findings rather than
    discard them. The subtype has no dedicated field on ``ResultMessage``, so the
    signal survives only in the error string ``_result_error`` assembles."""
    s = str(err or "").lower()
    return any(marker in s for marker in _MAX_TURNS_MARKERS)


# How Claude Code reports a `--resume` id whose conversation transcript is gone.
_RESUME_LOST_MARKERS = ("no conversation found",)
_SDK_STDERR_TAIL_LINES = 50


def _resume_lost(text: object) -> bool:
    """True when a failed resumed turn says its session id no longer exists — a
    dead id that would fail every later turn the same way (``resume_failed``)."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _RESUME_LOST_MARKERS)


def _result_error(msg: object, current: str = "") -> str:
    """The most actionable error string for a failed turn.

    ``ResultMessage.result`` carries the API's own sentence — "...does not
    support this model; version 2.1.251 or newer is required" — and it is the
    only part that tells an operator what to do, so it outranks everything,
    including an error already seen on an earlier message. That ordering is the
    whole point: on this failure the preceding ``AssistantMessage.error`` is the
    placeholder ``"unknown"`` and ``subtype`` is (misleadingly) ``"success"``,
    so taking the first thing that arrived reported "request failed: unknown"
    and left the model ladder with nothing to match on.

    ``current`` is the error already collected, used only when the result
    message carries no sentence of its own."""
    detail = getattr(msg, "result", None)
    detail = str(detail).strip() if isinstance(detail, str) else ""
    if detail:
        return detail
    status = getattr(msg, "api_error_status", None)
    return (
        current
        or (str(status) if status else "")
        or str(getattr(msg, "subtype", "") or "")
        or "result error"
    )


async def _run_sdk(
    message: str,
    timeout: float,
    resume: str | None,
    system_prompt: str,
    on_think: Callable[[str], None] | None,
    spec: ClaudeSpec,
    web_search: bool = False,
) -> OracleResult:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )

    try:  # ThinkingBlock is newer; degrade gracefully if the SDK lacks it
        from claude_agent_sdk import ThinkingBlock
    except ImportError:
        ThinkingBlock = ()  # type: ignore[assignment]

    try:  # the SDK's error types, mapped below; an SDK without them maps none
        from claude_agent_sdk import CLINotFoundError
    except ImportError:
        CLINotFoundError = ()  # type: ignore[assignment,misc]

    # The SDK only pipes the Claude Code process's stderr when a callback is set;
    # otherwise a failed process raises with no hint of WHY. Keep a bounded tail so a
    # dead resume id ("No conversation found") can be told apart from a transient
    # failure, and echo each line so the server log still shows it.
    stderr_tail: deque[str] = deque(maxlen=_SDK_STDERR_TAIL_LINES)

    def _on_stderr(line: str) -> None:
        stderr_tail.append(line)
        # Claude Code persists an MCP server's stderr to its own on-disk logs, so this
        # echo is a sink like any other — it goes through redaction (L5).
        print(redact_text(line)[0], file=sys.stderr)

    options = ClaudeAgentOptions(
        cli_path=best_cli_path(),  # None => let the SDK pick (its bundle, then PATH)
        model=spec.model,
        system_prompt=system_prompt,
        # Pure reasoning gets NO tools. The opt-in web-search path allows exactly
        # WebSearch/WebFetch (nothing else — no Bash/Read/Write), raises the turn
        # budget so the agentic search→answer loop can complete, and bypasses the
        # permission prompt so those two run headless (like grok's bypassPermissions).
        allowed_tools=(["WebSearch", "WebFetch"] if web_search else []),
        # The BASE tool set. `allowed_tools=[]` only blocks *use* — the descriptions
        # still ride in the system prompt (~109 KB of them). `tools=[]` is the SDK's
        # epilogue for `--tools ""`, which drops them entirely (the CLI path below
        # always did this; the SDK path was the leak).
        tools=(["WebSearch", "WebFetch"] if web_search else []),
        permission_mode=("bypassPermissions" if web_search else None),
        mcp_servers={},
        strict_mcp_config=True,  # ignore ambient MCP config
        max_turns=(websearch_max_turns() if web_search else 1),
        setting_sources=[],  # don't load user/project CLAUDE.md/settings
        env=_ISOLATION_ENV,  # auto-memory off
        extra_args=_ISOLATION_EXTRA_ARGS,  # --safe-mode: no repo customizations
        cwd=str(oracle_cwd()),  # neutral dir: nothing for discovery to find
        resume=resume,  # continue a prior conversation when set
        stderr=_on_stderr,
    )

    async def _drive() -> tuple[str, str, str | None, str | None, ProviderTelemetry]:
        parts: list[str] = []
        thinks: list[str] = []
        err: str | None = None
        session_id: str | None = None
        started = time.perf_counter()
        actual_model = spec.model
        request_id: str | None = None
        stop_reason: str | None = None
        usage: ProviderUsage | None = None
        api_duration_ms: float | None = None
        client = ClaudeSDKClient(options=options)
        await client.connect()
        try:
            await client.query(message)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    if getattr(msg, "error", None):
                        err = str(msg.error)
                    for block in msg.content or []:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                        elif ThinkingBlock and isinstance(block, ThinkingBlock):
                            chunk = getattr(block, "thinking", "") or ""
                            thinks.append(chunk)
                            if on_think and chunk:
                                try:
                                    on_think(chunk)
                                except Exception:  # noqa: BLE001 — a bad sink must not break the turn
                                    pass
                elif isinstance(msg, ResultMessage):
                    session_id = getattr(msg, "session_id", None)
                    actual_model = getattr(msg, "model", None) or spec.model
                    request_id = getattr(msg, "id", None) or getattr(msg, "result_id", None)
                    stop_reason = getattr(msg, "stop_reason", None) or getattr(msg, "subtype", None)
                    raw_usage = getattr(msg, "usage", None) or {}
                    usage = (
                        usage_if_available(
                            ProviderUsage(
                                input_tokens=token_count(raw_usage.get("input_tokens")),
                                output_tokens=token_count(raw_usage.get("output_tokens")),
                                cache_read_input_tokens=token_count(
                                    raw_usage.get("cache_read_input_tokens")
                                ),
                                cache_creation_input_tokens=token_count(
                                    raw_usage.get("cache_creation_input_tokens")
                                ),
                                cost_usd=normalize_cost_usd(getattr(msg, "total_cost_usd", None)),
                                # Claude Code's OAuth session is a flat plan:
                                # the SDK's figure is a list price, not spend.
                                cost_basis="subscription",
                            )
                        )
                        if isinstance(raw_usage, dict)
                        and (raw_usage or getattr(msg, "total_cost_usd", None) is not None)
                        else None
                    )
                    api_duration_ms = getattr(msg, "duration_api_ms", None)
                    if getattr(msg, "is_error", False):
                        err = _result_error(msg, err or "")
                    break
        finally:
            await client.disconnect()
        telemetry = ProviderTelemetry(
            oracle_key=spec.key,
            requested_model=spec.model,
            actual_model=actual_model,
            transport="agent-sdk",
            provider_request_id=request_id,
            provider_session_id=session_id,
            stop_reason=stop_reason,
            wall_duration_ms=(time.perf_counter() - started) * 1000,
            api_duration_ms=api_duration_ms,
            reasoning_available=bool(thinks),
            usage_available=usage is not None,
            tools_available=web_search,
            usage=usage,
        )
        return (
            "".join(parts).strip(),
            "\n".join(t for t in thinks if t).strip(),
            err,
            session_id,
            telemetry,
        )

    try:
        text, thinking, err, session_id, telemetry = await asyncio.wait_for(_drive(), timeout)
    except TimeoutError:
        return OracleResult(
            "error", kind="timeout", text=f"{spec.label} timed out after {timeout:.0f}s"
        )
    except ImportError:
        raise  # an SDK that cannot import its own parts is absent: _dispatch says so
    except CLINotFoundError as exc:
        # The SDK imported but has no Claude Code binary to spawn — the transport is
        # not THERE, the one failure `auto` may step past to the CLI.
        return OracleResult(
            "error", kind="binary_missing", model=spec.model,
            text=f"{spec.label} SDK: {exc} (install Claude Code, or point {CLI_ENV} at it)",
        )
    except Exception as exc:  # noqa: BLE001 — a bridge never raises at the oracle
        # The Claude Code process itself failed — a non-zero exit, a startup timeout,
        # a broken stream. The SDK raises these instead of returning a result, and
        # they used to escape run() into the tool's catch-all: no audit row, no
        # breaker record.
        kind, text = http_error_detail(
            label=f"{spec.label} SDK", error=f"{type(exc).__name__}: {exc}"
        )
        failed = OracleResult("error", kind=kind, text=text, model=spec.model)
        # Only a failure that SAYS the conversation is gone condemns the id — a 429,
        # an overload, a startup timeout or an expired login also kill the process,
        # and forgetting a live session on one of those loses the whole thread.
        if resume and _resume_lost(f"{exc}\n" + "\n".join(stderr_tail)):
            failed.meta["resume_failed"] = True
        return failed
    if err:
        # The agentic search loop hit its turn-budget wall mid-tool-use. The SDK
        # reports that as an error and we used to throw the whole turn away — 75+
        # seconds and real research discarded for a bare `error_max_turns`. Salvage
        # whatever prose was emitted as a TRUNCATED partial: `kind="truncated"`
        # rides the existing never-cached path (oracles.py), so a half-finished
        # research answer isn't pinned for the full TTL.
        if _is_max_turns_error(err) and text:
            return OracleResult(
                "ok",
                kind="truncated",
                text=text,
                thinking=thinking,
                session_id=session_id,
                telemetry=telemetry,
                meta={"partial": True, "stop_reason": "max_turns"},
            )
        # A provider-safeguard refusal arrives here as an ERROR (`is_error`), but
        # it is a refusal: classifying it `sdk_error` hid it from stats, inflated
        # this oracle's error rate toward a breaker trip, and handed the caller an
        # opaque error with no reframe recipe. `kind` carries the marker so the
        # handler attaches the provider-specific recipe while a plain model
        # refusal (the ``REFUSED:`` text contract) stays exactly as it was.
        provider_reason = is_provider_refusal(telemetry.stop_reason, str(err))
        if provider_reason is not None:
            return OracleResult(
                "refused",
                kind="provider_refusal",
                text=provider_reason,
                session_id=session_id,
                telemetry=telemetry,
            )
        # Surface the actual error (e.g. an api_error_status or "authentication
        # required" subtype) and keep the real telemetry — a constant text with
        # a 0ms stub made an expired login indistinguishable from any other
        # SDK failure in trace/audit.
        kind, text = http_error_detail(label=f"{spec.label} SDK", error=str(err))
        failed = OracleResult(
            "error", kind=kind, text=text, session_id=session_id, telemetry=telemetry
        )
        if resume and _resume_lost(err):
            failed.meta["resume_failed"] = True
        return failed
    # shape_stopped, not shape: a `max_tokens` stop is a cut-off answer (flagged
    # `truncated`, never cached) or, with no text, a spent budget — not a clean ok.
    res = shape_stopped(text, telemetry.stop_reason, label=spec.label, thinking=thinking)
    res.session_id = session_id
    res.thinking = thinking
    res.model = telemetry.actual_model or spec.model
    res.telemetry = telemetry
    return res


async def _run_cli(
    message: str,
    timeout: float,
    system_prompt: str,
    spec: ClaudeSpec,
    web_search: bool = False,
    resume: str | None = None,
) -> OracleResult:
    claude = shutil.which("claude")
    if not claude:
        return OracleResult(
            "error",
            kind="binary_missing",
            text="`claude` CLI not found on PATH (and the Claude Agent SDK was unavailable)",
        )
    argv = [
        claude,
        "-p",
        "--model",
        spec.model,
        "--system-prompt",
        system_prompt,
    ]
    if resume:
        # Continue the conversation the id names — the same flag the SDK passes.
        # Without it every follow-up ran as a fresh, memoryless thread that still
        # reported ok, and its new session id replaced the real one.
        argv += ["--resume", resume]
    if web_search:
        # Opt-in search fallback: make ONLY WebSearch/WebFetch available and let
        # them run headless. (The `claude` CLI has no --max-turns; print mode's
        # own agentic budget carries the search→answer loop. The SDK path above is
        # the primary, tested one — this is only reached when the SDK is absent.)
        argv += [
            "--tools",
            "WebSearch",
            "WebFetch",
            "--permission-mode",
            "bypassPermissions",
        ]
    else:
        argv += ["--tools", ""]  # disable every tool
    argv += [
        "--strict-mcp-config",  # + no --mcp-config => zero MCP tools
        "--output-format",
        "json",
        "--safe-mode",  # no CLAUDE.md/AGENTS.md/skills/plugins/hooks/agents
    ]

    try:
        started = time.perf_counter()
        run = await cli_gate.run_cli_async(
            argv,
            gate="claude",
            timeout=timeout,
            input_text=message,
            env=_ISOLATION_ENV,
            cwd=str(oracle_cwd()),
        )
    except FileNotFoundError:
        return OracleResult("error", kind="binary_missing", text="`claude` CLI not found on PATH")
    if run.queue_timed_out:
        return OracleResult(
            "error",
            kind="timeout",
            text=f"{spec.label} CLI queued behind ASK_FABLE_CLI_MAX_PARALLEL for {timeout:.0f}s "
            "without getting a slot",
        )
    if run.timed_out:
        return OracleResult(
            "error", kind="timeout", text=f"{spec.label} CLI timed out after {timeout:.0f}s"
        )
    returncode, stdout, stderr = run.returncode, run.stdout, run.stderr
    if returncode != 0:
        # A provider-safeguard refusal on the CLI transport carries no structured
        # stop_reason, so match on the sentence in stderr/stdout — the same
        # classification the SDK path makes, so `refused` means the same thing on
        # both transports.
        provider_reason = is_provider_refusal(None, f"{stderr}\n{stdout}")
        if provider_reason is not None:
            return OracleResult(
                "refused",
                kind="provider_refusal",
                text=provider_reason,
                returncode=returncode,
            )
        # Classify like every other CLI bridge — a discarded stderr and a
        # constant "Fable CLI failed" hid usage-limit/login errors and
        # misclassified them as generic sdk_error.
        kind, detail = cli_error_detail(
            label=spec.label, returncode=returncode, stderr=stderr, stdout=stdout
        )
        failed = OracleResult("error", kind=kind, text=detail, returncode=returncode)
        if resume and _resume_lost(f"{stderr}\n{stdout}"):
            failed.meta["resume_failed"] = True
        return failed
    answer = stdout
    actual_model = spec.model
    session_id: str | None = None
    stop_reason: str | None = None
    request_id: str | None = None
    usage: ProviderUsage | None = None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        answer = payload.get("result") or payload.get("text") or ""
        actual_model = payload.get("model") or spec.model
        session_id = payload.get("session_id")
        request_id = payload.get("id")
        stop_reason = payload.get("stop_reason") or payload.get("subtype")
        raw_usage = payload.get("usage") or {}
        usage = (
            usage_if_available(
                ProviderUsage(
                    input_tokens=token_count(raw_usage.get("input_tokens")),
                    output_tokens=token_count(raw_usage.get("output_tokens")),
                    cache_read_input_tokens=token_count(raw_usage.get("cache_read_input_tokens")),
                    cache_creation_input_tokens=token_count(
                        raw_usage.get("cache_creation_input_tokens")
                    ),
                    cost_usd=normalize_cost_usd(payload.get("total_cost_usd")),
                    cost_basis="subscription",  # same OAuth plan as the SDK path
                )
            )
            if isinstance(raw_usage, dict)
            and (raw_usage or payload.get("total_cost_usd") is not None)
            else None
        )
    telemetry = ProviderTelemetry(
        oracle_key=spec.key,
        requested_model=spec.model,
        actual_model=actual_model,
        transport="cli-json",
        provider_request_id=request_id,
        provider_session_id=session_id,
        stop_reason=stop_reason,
        returncode=returncode,
        wall_duration_ms=(time.perf_counter() - started) * 1000,
        reasoning_available=False,
        usage_available=usage is not None,
        tools_available=web_search,
        usage=usage,
    )
    if not isinstance(answer, str):
        return OracleResult(
            "error",
            kind="sdk_error",
            text=f"unrecognized {spec.label} CLI result shape",
            model=actual_model,
            session_id=session_id,
            telemetry=telemetry,
        )
    shaped = shape_stopped(answer, stop_reason, label=spec.label)
    shaped.session_id = session_id
    shaped.model = actual_model
    shaped.telemetry = telemetry
    return shaped
