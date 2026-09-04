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
environments without the SDK. The CLI fallback is single-turn only (text output
carries no resumable session id).

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
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from . import cli_gate
from .oracle_common import (
    OracleResult,
    cli_error_detail,
    compose,
    http_error_detail,
    shape,
    timeout_default,
)
from .prompts import FABLE_SYSTEM_PROMPT
from .provider_telemetry import (
    ProviderTelemetry,
    ProviderUsage,
    normalize_cost_usd,
    token_count,
    usage_if_available,
)

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

# Model ids this process has seen a transport reject. Process-scoped on purpose:
# it is a fact about the local Claude Code build, so it must not outlive an
# upgrade, and re-probing once per server start is cheap.
_unavailable: set[str] = set()


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


def pinned_model() -> str:
    """The operator's exact-id pin from ``ASK_FABLE_FABLE_MODEL``, or ""."""
    return (os.environ.get(MODEL_ENV) or "").strip()


def fable_model() -> str:
    """The Fable model id this process will actually ask for.

    A pin wins outright; otherwise the newest candidate not yet demoted. Read at
    call time so an operator can pin (or unpin) without restarting the server."""
    pin = pinned_model()
    if pin:
        return pin
    for candidate in FABLE_CANDIDATES:
        if candidate not in _unavailable:
            return candidate
    return FABLE_MODEL


def fable_spec() -> ClaudeSpec:
    """The `fable` spec with its model resolved through the ladder. The label
    stays "Fable" at every rung — which id answered is already reported by
    ``OracleResult.model`` and ``ProviderTelemetry.actual_model``."""
    return ClaudeSpec(model=fable_model(), key="fable", label="Fable")


def _demote(model: str) -> str | None:
    """Record that a transport rejected ``model``; return the next id to try."""
    _unavailable.add(model)
    for candidate in FABLE_CANDIDATES:
        if candidate not in _unavailable:
            return candidate
    return None


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _cli_version(path: str) -> tuple[int, ...] | None:
    """`<path> --version` parsed into a comparable tuple, or None if it won't run."""
    try:
        out = subprocess.run(  # noqa: S603 — a Claude Code binary we resolved ourselves
            [path, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
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
) -> OracleResult:
    """Run one Fable turn. Returns an OracleResult (never raises for expected
    failures). ``resume`` continues a prior SDK session (ignored by the CLI path).
    ``system_prompt`` overrides the default oracle prompt (used for synthesis).
    ``on_think`` is a best-effort sink called with each reasoning block as it streams
    from the SDK (for a live console trace); ignored by the CLI path.
    ``spec`` selects the Anthropic model. Omit it for Fable resolved through the
    ladder (``fable_spec``); ``opus.run`` and ``fable51.run`` pass their own spec
    to reach another model over the same OAuth session.

    Only a laddered call falls back: if the transport rejects the model outright
    (``model_unavailable`` — an older Claude Code build, typically), that id is
    demoted for the life of the process and the turn is retried one rung down.
    An explicit spec or an ``ASK_FABLE_FABLE_MODEL`` pin is honored as written —
    silently answering as a different model than the operator named would make
    an A/B meaningless."""
    timeout = timeout if timeout is not None else timeout_default()
    system_prompt = system_prompt or FABLE_SYSTEM_PROMPT
    if use_cli is None:
        use_cli = (os.environ.get("ASK_FABLE_USE_CLI") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    laddered = spec is None and not pinned_model()
    spec = spec if spec is not None else fable_spec()
    message = compose(question, context)
    result = await _dispatch(message, timeout, resume, system_prompt, on_think, spec, use_cli)
    if laddered and result.kind == "model_unavailable":
        nxt = _demote(spec.model)
        if nxt is not None:
            # `resume` is dropped: an SDK session id is bound to the model that
            # created it (the same reason `ask_opus5` namespaces its sessions),
            # so the fallback starts a fresh thread rather than resuming another
            # model's conversation.
            result = await _dispatch(
                message, timeout, None, system_prompt, on_think,
                replace(spec, model=nxt), use_cli,
            )
    return result


async def _dispatch(
    message: str,
    timeout: float,
    resume: str | None,
    system_prompt: str,
    on_think: Callable[[str], None] | None,
    spec: ClaudeSpec,
    use_cli: bool,
) -> OracleResult:
    """One turn on one model over whichever transport is available."""
    if use_cli:
        return await _run_cli(message, timeout, system_prompt, spec)
    try:
        return await _run_sdk(message, timeout, resume, system_prompt, on_think, spec)
    except ImportError:
        # SDK unavailable — degrade to the CLI bridge (single-turn, no live stream).
        return await _run_cli(message, timeout, system_prompt, spec)


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

    options = ClaudeAgentOptions(
        cli_path=best_cli_path(),  # None => let the SDK pick (its bundle, then PATH)
        model=spec.model,
        system_prompt=system_prompt,
        allowed_tools=[],  # pure reasoning — no tools
        mcp_servers={},
        strict_mcp_config=True,  # ignore ambient MCP config
        max_turns=1,
        setting_sources=[],  # don't load user/project CLAUDE.md/settings
        resume=resume,  # continue a prior conversation when set
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
            tools_available=False,
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
    if err:
        # Surface the actual error (e.g. an api_error_status or "authentication
        # required" subtype) and keep the real telemetry — a constant text with
        # a 0ms stub made an expired login indistinguishable from any other
        # SDK failure in trace/audit.
        kind, text = http_error_detail(label=f"{spec.label} SDK", error=str(err))
        return OracleResult(
            "error", kind=kind, text=text, session_id=session_id, telemetry=telemetry
        )
    res = shape(text, session_id)
    res.thinking = thinking
    res.model = telemetry.actual_model or spec.model
    res.telemetry = telemetry
    return res


async def _run_cli(
    message: str,
    timeout: float,
    system_prompt: str,
    spec: ClaudeSpec,
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
        "--tools",
        "",  # disable every tool
        "--strict-mcp-config",  # + no --mcp-config => zero MCP tools
        "--output-format",
        "json",
    ]

    try:
        started = time.perf_counter()
        run = await cli_gate.run_cli_async(
            argv, gate="claude", timeout=timeout, input_text=message
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
        # Classify like every other CLI bridge — a discarded stderr and a
        # constant "Fable CLI failed" hid usage-limit/login errors and
        # misclassified them as generic sdk_error.
        kind, detail = cli_error_detail(
            label=spec.label, returncode=returncode, stderr=stderr, stdout=stdout
        )
        return OracleResult("error", kind=kind, text=detail, returncode=returncode)
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
        tools_available=False,
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
    shaped = shape(answer, session_id)
    shaped.model = actual_model
    shaped.telemetry = telemetry
    return shaped
