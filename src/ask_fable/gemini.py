"""Invoke a Gemini model (Gemini 3.1 Pro by default) for one reasoning turn.

Another single-model oracle, reached the same way as MiniMax: by shelling out to
an already-authenticated CLI — here the ``agy`` agent CLI, which fronts Google's
Gemini (plus other) models. Like the ``mmx`` and ``claude`` bridges we set NO API
key and re-use the host login. ``agy --print/-p`` runs a single prompt
non-interactively and prints the plain-text answer on stdout; ``--model`` selects
the model (its display name, e.g. ``Gemini 3.1 Pro (High)``).

The prompt is passed as the ``-p`` argument (``agy`` has no stdin prompt mode), so
the shared scope system prompt is prepended into that text. There's no ``--system``
channel and no JSON envelope — stdout is the raw answer — and the same ``REFUSED:``
scope contract Fable/MiniMax use is applied via ``shape``.

``agy`` is an agent CLI that (a) reads stdin as extra context and (b) can spawn
child processes. Two subprocess hazards follow. First, if it inherits the caller's
stdin it blocks reading it — under the MCP server that stdin is the open JSON-RPC
pipe, which never EOFs, so a turn hangs to the timeout; we hand it
``stdin=DEVNULL`` for an immediate EOF. Second, a plain ``subprocess.run`` timeout
SIGKILLs only the direct child, leaving grandchildren holding the stdout pipe open
so ``communicate`` blocks forever and orphans leak; we launch it in its own session
and, on timeout, SIGKILL the whole process group. ``agy``'s own ``--print-timeout``
is a softer self-limit inside that hard backstop.

Pure reasoning, single-turn: no session to resume.
"""

from __future__ import annotations

import os
import shutil
import time

from . import cli_gate
from .oracle_common import OracleResult, cli_error_detail, compose, gemini_timeout_default, shape
from .prompts import ORACLE_SYSTEM_PROMPT
from .provider_telemetry import ProviderTelemetry

DEFAULT_GEMINI_MODEL = "Gemini 3.1 Pro (High)"
GEMINI_BINARY = "agy"


def gemini_model() -> str:
    return (os.environ.get("ASK_FABLE_GEMINI_MODEL") or "").strip() or DEFAULT_GEMINI_MODEL


def _error_telemetry(
    model: str, *, wall_duration_ms: float = 0.0, returncode: int | None = None
) -> ProviderTelemetry:
    return ProviderTelemetry(
        oracle_key="gemini", requested_model=model, actual_model=model,
        transport="cli-text", returncode=returncode, wall_duration_ms=wall_duration_ms,
        reasoning_available=None, usage_available=None, tools_available=None,
    )


async def run(
    question: str,
    context: str = "",
    *,
    timeout: float | None = None,
    model: str | None = None,
) -> OracleResult:
    """Run one Gemini turn via the ``agy`` CLI. Never raises for expected failures."""
    timeout = timeout if timeout is not None else gemini_timeout_default()
    model = model or gemini_model()

    agy = shutil.which(GEMINI_BINARY)
    if not agy:
        return OracleResult(
            "error",
            kind="binary_missing",
            text=f"`{GEMINI_BINARY}` CLI not found on PATH (install it and sign in to use ask_gemini)",
            model=model,
            telemetry=_error_telemetry(model),
        )

    # No --system channel and no stdin prompt mode — fold the scope prompt into the
    # single -p prompt argument. Give agy a soft self-limit just inside our hard kill.
    message = f"{ORACLE_SYSTEM_PROMPT}\n\n{compose(question, context)}"
    agy_budget = max(30, int(timeout) - 10)
    argv = [agy, "--model", model, "--print-timeout", f"{agy_budget}s", "-p", message]

    try:
        started = time.perf_counter()
        cap = await cli_gate.run_cli_async(argv, gate="agy", timeout=timeout)
    except FileNotFoundError:  # binary vanished between which() and spawn
        return OracleResult(
            "error", kind="binary_missing", text=f"`{GEMINI_BINARY}` CLI not found on PATH",
            model=model, telemetry=_error_telemetry(model),
        )

    if cap.timed_out:
        detail = (
            f"Gemini queued behind ASK_FABLE_CLI_MAX_PARALLEL for {timeout:.0f}s "
            "without getting a slot"
            if cap.queue_timed_out
            else f"Gemini timed out after {timeout:.0f}s"
        )
        return OracleResult(
            "error", kind="timeout", text=detail, model=model,
            telemetry=_error_telemetry(model, wall_duration_ms=(time.perf_counter() - started) * 1000),
        )
    if cap.returncode != 0:
        kind, detail = cli_error_detail(
            label="Gemini",
            returncode=cap.returncode,
            stderr=cap.stderr,
            stdout=cap.stdout,
        )
        return OracleResult(
            "error", kind=kind, text=detail, model=model,
            returncode=cap.returncode,
            telemetry=_error_telemetry(
                model, wall_duration_ms=(time.perf_counter() - started) * 1000,
                returncode=cap.returncode,
            ),
        )

    answer = cap.stdout.strip()
    if not answer:
        return OracleResult(
            "error", kind="sdk_error", text="Gemini returned no answer", model=model,
            telemetry=_error_telemetry(model, wall_duration_ms=(time.perf_counter() - started) * 1000),
        )
    shaped = shape(answer)  # honor the shared REFUSED contract
    shaped.model = model
    shaped.telemetry = ProviderTelemetry(
        oracle_key="gemini", requested_model=model, actual_model=model,
        transport="cli-text", returncode=cap.returncode,
        wall_duration_ms=(time.perf_counter() - started) * 1000,
        reasoning_available=None, usage_available=None, tools_available=None,
    )
    return shaped
