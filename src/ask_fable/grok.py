"""Invoke an xAI Grok model (grok-4.6 by default) for one reasoning turn.

Another single-model oracle, reached the same way as MiniMax / Gemini / Codex:
shell out to an already-authenticated CLI — here the local ``grok`` binary
(Grok Build TUI) in single-turn ``-p`` / ``--single`` mode. No API key is set;
we re-use the host login (``grok login`` / grok.com session).

Prefer this bridge over ``ask_atlas`` with ``xai/grok-*`` whenever the binary
is on PATH: same model family, no Atlas API key, operator's existing login.

The invocation is deliberately HERMETIC so it behaves as a pure reasoning
oracle rather than a repo-editing agent:

- ``--max-turns 6`` — a small agentic budget, NOT 1. grok-4.6's ``-p`` mode is an
  agentic loop: on a repo/context-flavored question it reflexively spends its
  early turns planning a repo inspection ("first I'll check the repo…") instead of
  answering, so ``--max-turns 1`` guillotines it before the answer and the CLI
  exits rc=1 "Max turns reached". At the default ``high`` reasoning effort it needs
  ~6 turns to burn the gather reflex and still produce the final answer (~30s);
  4 is not enough at high effort, and generic questions answer in turn 1 regardless.
- ``--permission-mode bypassPermissions`` + disallowed tool list — no tool
  execution (context is passed inline)
- ``--no-subagents`` / ``--no-memory`` / ``--no-plan`` / ``--disable-web-search``
- ``--system-prompt-override`` carries the shared scope contract
- ``--output-format plain`` — stdout is the raw answer

Like ``agy`` / ``codex``, the CLI can spawn children and would block forever on
the MCP server's open stdin pipe, so we use ``stdin=DEVNULL`` and a process-group
SIGKILL on timeout.

Pure reasoning: no session to resume.
"""

from __future__ import annotations

import os
import shutil
import time

from . import cli_gate
from .oracle_common import OracleResult, cli_error_detail, compose, shape, timeout_default
from .prompts import ORACLE_SYSTEM_PROMPT
from .provider_telemetry import ProviderTelemetry

DEFAULT_GROK_MODEL = "grok-4.6"
# `low`, NOT high. grok's agentic `-p` mode reflexively "gathers repo context"
# on context-heavy questions; at high effort that gather loop is nondeterministic
# and spirals until the wall-clock timeout instead of answering. Low effort keeps
# it from spiralling so it answers within the turn budget (~30-60s, verified).
# Override with ASK_FABLE_GROK_REASONING when a caller genuinely wants deep grok.
DEFAULT_GROK_REASONING = "low"
GROK_BINARY = "grok"

# Map Atlas-style effort presets (and plain reasoning names) → grok --reasoning-effort.
# The Atlas presets (quick/standard/deep) map to `low`, NOT high: grok's agentic
# `-p` mode spirals on context-heavy questions at high effort (see the header +
# DEFAULT_GROK_REASONING), and the atlas→grok routing default of "deep" would
# otherwise re-trigger that. Only grok-native `medium`/`high` (reachable via an
# explicit ASK_FABLE_GROK_REASONING / ASK_FABLE_EFFORT) opt into high.
_EFFORT_MAP = {
    "quick": "low",
    "standard": "low",
    "deep": "low",
    "low": "low",
    "medium": "high",  # explicit grok-native request
    "high": "high",     # explicit grok-native request
}

# Built-in tools stripped so the turn is pure text reasoning (context is in the prompt).
_DISALLOWED_TOOLS = (
    "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Agent,"
    "image_gen,image_edit,image_to_video,reference_to_video,"
    "run_terminal_command,search_replace,spawn_subagent"
)


def grok_model() -> str:
    return (os.environ.get("ASK_FABLE_GROK_MODEL") or "").strip() or DEFAULT_GROK_MODEL


def grok_reasoning() -> str:
    """Default ``--reasoning-effort`` when no per-call effort is passed.

    Precedence: ``ASK_FABLE_GROK_REASONING`` → ``ASK_FABLE_EFFORT`` (mapped through
    ``_EFFORT_MAP``) → ``high``.
    """
    explicit = (os.environ.get("ASK_FABLE_GROK_REASONING") or "").strip()
    if explicit:
        return explicit
    global_effort = (os.environ.get("ASK_FABLE_EFFORT") or "").strip().lower()
    if global_effort:
        return _EFFORT_MAP.get(global_effort, DEFAULT_GROK_REASONING)
    return DEFAULT_GROK_REASONING


def grok_timeout_default() -> float:
    """Prefer ``ASK_FABLE_GROK_TIMEOUT`` so the agentic CLI can be capped without
    lowering the global timeout."""
    for var in ("ASK_FABLE_GROK_TIMEOUT", "ASK_FABLE_TIMEOUT"):
        raw = os.environ.get(var)
        if raw:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    return timeout_default()


def available() -> bool:
    return shutil.which(GROK_BINARY) is not None


def looks_like_grok_model(model: str) -> bool:
    """True when a gateway (or bare) model id is an xAI Grok model we can serve locally.

    Gateways spell the vendor differently — Atlas uses ``xai/``, OpenRouter uses
    ``x-ai/`` — so both prefixes are stripped before the check. Missing one means
    the id quietly bills through the gateway when the operator's own
    already-authenticated CLI could have answered for free."""
    m = (model or "").strip().lower()
    if not m:
        return False
    for prefix in ("xai/", "x-ai/"):
        if m.startswith(prefix):
            m = m[len(prefix):]
            break
    return m.startswith("grok")


def _error_telemetry(
    model: str, *, wall_duration_ms: float = 0.0, returncode: int | None = None
) -> ProviderTelemetry:
    return ProviderTelemetry(
        oracle_key="grok", requested_model=model, actual_model=model,
        transport="cli-text", returncode=returncode, wall_duration_ms=wall_duration_ms,
        reasoning_available=None, usage_available=None, tools_available=False,
    )


async def run(
    question: str,
    context: str = "",
    *,
    timeout: float | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> OracleResult:
    """Run one Grok turn via the local ``grok`` CLI. Never raises for expected failures."""
    timeout = timeout if timeout is not None else grok_timeout_default()
    model = model or grok_model()
    # Accept Atlas-style ids (xai/grok-4.6) by stripping the provider prefix.
    if model.lower().startswith("xai/"):
        model = model.split("/", 1)[1]
    raw_effort = (effort or "").strip().lower()
    reasoning = _EFFORT_MAP.get(raw_effort, raw_effort or grok_reasoning())

    grok_bin = shutil.which(GROK_BINARY)
    if not grok_bin:
        return OracleResult(
            "error",
            kind="binary_missing",
            text=(
                f"`{GROK_BINARY}` CLI not found on PATH "
                "(install Grok Build and run `grok login` to use ask_grok)"
            ),
            model=model,
            telemetry=_error_telemetry(model),
        )

    # Scope contract via system override; user turn is question + code context.
    message = compose(question, context)
    argv = [
        grok_bin,
        "-p", message,
        "--output-format", "plain",
        # NOT 1: grok's -p is agentic and spends early turns on a repo-inspection
        # plan for context-flavored questions; 1 aborts as "Max turns reached", and
        # 4 is still too few at the default high effort — 6 lets it finish (~30s).
        "--max-turns", "6",
        "-m", model,
        "--reasoning-effort", reasoning,
        "--system-prompt-override", ORACLE_SYSTEM_PROMPT,
        "--permission-mode", "bypassPermissions",
        "--no-subagents",
        "--no-memory",
        "--no-plan",
        "--disable-web-search",
        "--disallowed-tools", _DISALLOWED_TOOLS,
    ]

    try:
        started = time.perf_counter()
        cap = await cli_gate.run_cli_async(argv, gate="grok", timeout=timeout)
    except FileNotFoundError:
        return OracleResult(
            "error", kind="binary_missing",
            text=f"`{GROK_BINARY}` CLI not found on PATH",
            model=model, telemetry=_error_telemetry(model),
        )

    wall_ms = (time.perf_counter() - started) * 1000
    if cap.timed_out:
        detail = (
            f"Grok queued behind ASK_FABLE_CLI_MAX_PARALLEL for {timeout:.0f}s "
            "without getting a slot"
            if cap.queue_timed_out
            else f"Grok timed out after {timeout:.0f}s"
        )
        return OracleResult(
            "error", kind="timeout", text=detail, model=model,
            telemetry=_error_telemetry(model, wall_duration_ms=wall_ms),
        )
    if cap.returncode != 0:
        kind, detail = cli_error_detail(
            label="Grok",
            returncode=cap.returncode,
            stderr=cap.stderr,
            stdout=cap.stdout,
        )
        return OracleResult(
            "error", kind=kind, text=detail, model=model,
            returncode=cap.returncode,
            telemetry=_error_telemetry(model, wall_duration_ms=wall_ms, returncode=cap.returncode),
        )

    answer = cap.stdout.strip()
    if not answer:
        return OracleResult(
            "error", kind="sdk_error", text="Grok returned no answer", model=model,
            telemetry=_error_telemetry(model, wall_duration_ms=wall_ms),
        )
    shaped = shape(answer)
    shaped.model = model
    shaped.telemetry = ProviderTelemetry(
        oracle_key="grok", requested_model=model, actual_model=model,
        transport="cli-text", returncode=cap.returncode,
        wall_duration_ms=wall_ms,
        reasoning_available=None, usage_available=None, tools_available=False,
    )
    return shaped
