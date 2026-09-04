"""Invoke a MiniMax model (MiniMax-M3 by default) for one reasoning turn.

The second oracle in the ``ask_council`` fan-out. It shells out to the ``mmx``
CLI (``mmx text chat``), which is already API-key authenticated on the host — so,
exactly like the Fable CLI bridge, we set NO key here and re-use the existing
session. ``--output json`` returns an Anthropic-Messages-style envelope
(``content[]`` with ``text`` and ``thinking`` blocks + ``base_resp.status_code``);
we extract the answer text (and the reasoning, for the console) and apply the same
``REFUSED:`` scope contract Fable uses.

Pure reasoning: no MiniMax tools are enabled, we only send a system prompt and a
single user message on stdin. Single-turn (no server-side session to resume).
"""

from __future__ import annotations

import json
import os
import shutil
import time

from . import cli_gate
from .oracle_common import (
    OracleResult,
    cli_error_detail,
    compose,
    max_tokens,
    parse_anthropic_envelope,
    shape,
    timeout_default,
)
from .prompts import ORACLE_SYSTEM_PROMPT
from .provider_telemetry import ProviderTelemetry, ProviderUsage, token_count, usage_if_available

DEFAULT_MINIMAX_MODEL = "MiniMax-M3"


def minimax_model() -> str:
    return (os.environ.get("ASK_FABLE_MINIMAX_MODEL") or "").strip() or DEFAULT_MINIMAX_MODEL


def _extract(stdout: str) -> tuple[str | None, str, str | None]:
    """Parse an ``mmx text chat --output json`` envelope.

    Returns (answer_text, thinking, error). On a well-formed success envelope
    ``answer_text`` is the joined text blocks and ``thinking`` the joined thinking
    blocks; on an API-level failure ``error`` carries the reason.
    """
    stdout = (stdout or "").strip()
    if not stdout:
        return None, "", "empty response from mmx"
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        # Some mmx builds emit plain text even for --output json; take it as the answer.
        return stdout, "", None
    base = obj.get("base_resp") or {}
    code = base.get("status_code")
    if code not in (None, 0):
        return None, "", f"MiniMax API error {code}: {base.get('status_msg') or 'unknown'}"
    text, thinking, err = parse_anthropic_envelope(obj)
    if err:
        # Fall through to legacy shapes if the shared parser didn't recognize it.
        if isinstance(obj.get("text"), str):
            return obj["text"].strip(), "", None
        return (
            None,
            "",
            err
            if err != "unrecognized response shape (no content[])"
            else "unrecognized mmx response shape",
        )
    return text, thinking, None


async def run(
    question: str,
    context: str = "",
    *,
    timeout: float | None = None,
    model: str | None = None,
) -> OracleResult:
    """Run one MiniMax turn via the ``mmx`` CLI. Never raises for expected failures."""
    timeout = timeout if timeout is not None else timeout_default()
    model = model or minimax_model()

    mmx = shutil.which("mmx")
    if not mmx:
        return OracleResult(
            "error",
            kind="binary_missing",
            text="`mmx` CLI not found on PATH (install it and run `mmx auth login`)",
            model=model,
        )

    message = compose(question, context)
    messages = json.dumps([{"role": "user", "content": message}])
    argv = [
        mmx,
        "text",
        "chat",
        "--model",
        model,
        "--max-tokens",
        str(max_tokens()),
        "--system",
        ORACLE_SYSTEM_PROMPT,
        "--messages-file",
        "-",  # messages JSON on stdin — no ARG_MAX limit, no argv injection
        "--output",
        "json",
    ]

    try:
        started = time.perf_counter()
        run = await cli_gate.run_cli_async(argv, gate="mmx", timeout=timeout, input_text=messages)
    except FileNotFoundError:
        return OracleResult(
            "error", kind="binary_missing", text="`mmx` CLI not found on PATH", model=model
        )
    if run.queue_timed_out:
        return OracleResult(
            "error",
            kind="timeout",
            text=f"MiniMax queued behind ASK_FABLE_CLI_MAX_PARALLEL for {timeout:.0f}s "
            "without getting a slot",
            model=model,
        )
    if run.timed_out:
        return OracleResult(
            "error", kind="timeout", text=f"MiniMax timed out after {timeout:.0f}s", model=model
        )
    rc, stdout, stderr = run.returncode, run.stdout, run.stderr
    if rc != 0:
        kind, detail = cli_error_detail(
            label="MiniMax",
            returncode=rc,
            stderr=stderr,
            stdout=stdout,
        )
        return OracleResult(
            "error", kind=kind, text=detail, model=model,
            returncode=rc,
        )

    text, thinking, err = _extract(stdout)
    if err:
        # API errors can arrive as rc=0 with base_resp.status_code set — classify.
        kind, detail = cli_error_detail(
            label="MiniMax", returncode=rc, stderr=err, stdout=stdout,
        )
        return OracleResult("error", kind=kind, text=detail, model=model)
    shaped = shape(text)  # honor the shared REFUSED contract
    shaped.model = model
    shaped.thinking = thinking
    actual_model = model
    stop_reason: str | None = None
    request_id: str | None = None
    usage: ProviderUsage | None = None
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        envelope = {}
    if isinstance(envelope, dict):
        actual_model = envelope.get("model") or model
        stop_reason = envelope.get("stop_reason")
        request_id = envelope.get("id")
        raw_usage = envelope.get("usage") or {}
        if isinstance(raw_usage, dict) and raw_usage:
            usage = usage_if_available(
                ProviderUsage(
                    input_tokens=token_count(raw_usage.get("input_tokens")),
                    output_tokens=token_count(raw_usage.get("output_tokens")),
                    cache_read_input_tokens=token_count(raw_usage.get("cache_read_input_tokens")),
                    cache_creation_input_tokens=token_count(
                        raw_usage.get("cache_creation_input_tokens")
                    ),
                )
            )
    shaped.telemetry = ProviderTelemetry(
        oracle_key="minimax",
        requested_model=model,
        actual_model=actual_model,
        transport="cli-json",
        provider_request_id=request_id,
        stop_reason=stop_reason,
        wall_duration_ms=(time.perf_counter() - started) * 1000,
        reasoning_available=bool(thinking),
        usage_available=usage is not None,
        tools_available=False,
        usage=usage,
    )
    return shaped
