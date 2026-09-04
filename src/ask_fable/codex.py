"""Invoke an OpenAI model (GPT-5.6 Sol by default) for one reasoning turn.

Another single-model oracle, reached the same way as MiniMax and Gemini: by
shelling out to an already-authenticated CLI — here OpenAI's ``codex`` CLI in its
non-interactive ``codex exec`` mode. Like the ``mmx`` and ``agy`` bridges we set NO
API key and re-use the host login (``codex login``). ``codex exec <prompt>`` runs a
single prompt non-interactively; the agent's final message is written to the file
given by ``--output-last-message`` (and also echoed on stdout), while all the
progress chrome (banner, session id, hook lines) goes to stderr.

The invocation is deliberately HERMETIC so it behaves as a pure reasoning oracle
rather than a repo-editing agent:

- ``--ignore-user-config`` skips ``~/.codex/config.toml`` (auth still uses
  ``CODEX_HOME``), so the operator's own model / sandbox / hooks never leak in and
  change the answer. We then set every knob we care about explicitly.
- ``--sandbox read-only`` + ``--skip-git-repo-check`` + ``--ephemeral``: the model
  can't see this repo anyway (context is passed inline), so give it no write access,
  don't require a git repo (the MCP server's cwd may not be one), and don't persist
  a session file.
- ``-c model_reasoning_effort=<effort>`` sets the reasoning budget (default high),
  since ignoring user config resets it to none.

``codex exec`` has no ``--system`` channel, so the shared scope system prompt is
folded into the single prompt argument, and the same ``REFUSED:`` scope contract
Fable/MiniMax/Gemini use is applied via ``shape``.

``codex`` is an agent CLI that (a) reads stdin as extra context and (b) can spawn
child processes, so it carries the same two subprocess hazards as ``agy`` and we
handle them identically: ``stdin=DEVNULL`` (otherwise it blocks reading the MCP
server's open JSON-RPC pipe, which never EOFs) and its own session + a group
SIGKILL on timeout (otherwise grandchildren hold the stdout pipe open and orphan).

Pure reasoning, single-turn: no session to resume.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time

from . import cli_gate
from .oracle_common import OracleResult, cli_error_detail, codex_timeout_default, compose, shape
from .prompts import ORACLE_SYSTEM_PROMPT
from .provider_telemetry import (
    ProviderTelemetry,
    ProviderUsage,
    ToolEvent,
    token_count,
    usage_if_available,
)

DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_REASONING = "high"
CODEX_BINARY = "codex"


def codex_model() -> str:
    return (os.environ.get("ASK_FABLE_CODEX_MODEL") or "").strip() or DEFAULT_CODEX_MODEL


def codex_reasoning() -> str:
    return (os.environ.get("ASK_FABLE_CODEX_REASONING") or "").strip() or DEFAULT_CODEX_REASONING


def _error_telemetry(
    model: str, *, wall_duration_ms: float = 0.0, returncode: int | None = None
) -> ProviderTelemetry:
    return ProviderTelemetry(
        oracle_key="codex", requested_model=model, actual_model=model,
        transport="cli-jsonl", returncode=returncode, wall_duration_ms=wall_duration_ms,
        reasoning_available=False, usage_available=False, tools_available=True,
    )


def _parse_jsonl(stdout: str, model: str, wall_duration_ms: float) -> tuple[str, ProviderTelemetry]:
    session_id: str | None = None
    thinking: list[str] = []
    usage: ProviderUsage | None = None
    tools: list[ToolEvent] = []
    unknown: list[str] = []
    known = {"thread.started", "item.completed", "turn.completed", "turn.failed", "error"}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            unknown.append(hashlib.sha256(line.encode()).hexdigest()[:16])
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            session_id = event.get("thread_id")
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if not isinstance(item, dict):
                unknown.append(hashlib.sha256(line.encode()).hexdigest()[:16])
                continue
            item_type = item.get("type")
            if item_type == "reasoning" and item.get("text"):
                thinking.append(str(item["text"]))
            elif item_type in {"command_execution", "mcp_tool_call"}:
                tools.append(
                    ToolEvent(
                        name=item_type,
                        call_id=item.get("id"),
                        status=item.get("status"),
                        exit_code=item.get("exit_code"),
                    )
                )
        elif event_type == "turn.completed":
            raw = event.get("usage") or {}
            if not isinstance(raw, dict):
                raw = {}
            input_tokens = token_count(raw.get("input_tokens"))
            output_tokens = token_count(raw.get("output_tokens"))
            usage = usage_if_available(
                ProviderUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=(input_tokens + output_tokens)
                    if isinstance(input_tokens, int) and isinstance(output_tokens, int)
                    else None,
                )
            )
        if not isinstance(event_type, str) or event_type not in known:
            unknown.append(hashlib.sha256(line.encode()).hexdigest()[:16])
    telemetry = ProviderTelemetry(
        oracle_key="codex",
        requested_model=model,
        actual_model=model,
        transport="cli-jsonl",
        provider_session_id=session_id,
        wall_duration_ms=wall_duration_ms,
        reasoning_available=bool(thinking),
        usage_available=usage is not None,
        tools_available=True,
        unknown_event_count=len(unknown),
        unknown_event_fingerprints=tuple(unknown),
        usage=usage,
        tool_events=tuple(tools),
    )
    return "\n".join(thinking), telemetry


async def run(
    question: str,
    context: str = "",
    *,
    timeout: float | None = None,
    model: str | None = None,
) -> OracleResult:
    """Run one OpenAI turn via the ``codex exec`` CLI. Never raises for expected failures."""
    timeout = timeout if timeout is not None else codex_timeout_default()
    model = model or codex_model()

    codex = shutil.which(CODEX_BINARY)
    if not codex:
        return OracleResult(
            "error",
            kind="binary_missing",
            text=f"`{CODEX_BINARY}` CLI not found on PATH (install it and run `codex login` to use ask_codex)",
            model=model,
            telemetry=_error_telemetry(model),
        )

    # No --system channel — fold the scope prompt into the single prompt argument.
    message = f"{ORACLE_SYSTEM_PROMPT}\n\n{compose(question, context)}"
    # The final agent message is captured from a temp file (--output-last-message);
    # stdout carries it too but the file is the unambiguous source.
    out_fd, out_path = tempfile.mkstemp(prefix="ask_codex_", suffix=".txt")
    os.close(out_fd)
    argv = [
        codex,
        "exec",
        "--ignore-user-config",  # hermetic: our knobs only, not the operator's config/hooks
        "--model",
        model,
        "-c",
        f"model_reasoning_effort={codex_reasoning()}",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "--json",
        "-o",
        out_path,
        message,
    ]

    try:
        started = time.perf_counter()
        try:
            cap = await cli_gate.run_cli_async(argv, gate="codex", timeout=timeout)
        except FileNotFoundError:  # binary vanished between which() and spawn
            return OracleResult(
                "error",
                kind="binary_missing",
                text=f"`{CODEX_BINARY}` CLI not found on PATH",
                model=model,
                telemetry=_error_telemetry(model, wall_duration_ms=(time.perf_counter() - started) * 1000),
            )

        if cap.timed_out:
            detail = (
                f"Codex queued behind ASK_FABLE_CLI_MAX_PARALLEL for {timeout:.0f}s "
                "without getting a slot"
                if cap.queue_timed_out
                else f"Codex timed out after {timeout:.0f}s"
            )
            return OracleResult(
                "error", kind="timeout", text=detail, model=model,
                telemetry=_error_telemetry(model, wall_duration_ms=(time.perf_counter() - started) * 1000),
            )
        if cap.returncode != 0:
            kind, detail = cli_error_detail(
                label="Codex",
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

        answer = ""
        try:
            with open(out_path, encoding="utf-8") as fh:
                answer = fh.read().strip()
        except OSError:
            answer = ""
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    if not answer:
        return OracleResult(
            "error", kind="sdk_error", text="Codex returned no answer", model=model,
            telemetry=_error_telemetry(model, wall_duration_ms=(time.perf_counter() - started) * 1000),
        )
    shaped = shape(answer)  # honor the shared REFUSED contract
    shaped.model = model
    shaped.thinking, shaped.telemetry = _parse_jsonl(
        cap.stdout,
        model,
        (time.perf_counter() - started) * 1000,
    )
    return shaped
