"""Shared helpers for every oracle bridge — message framing, REFUSED shaping,
timeout/token defaults, and Anthropic-envelope parsing.

Centralized so the five bridge modules (``fable``, ``minimax``, ``gemini``,
``anthropic_http``, ``ollama``) don't each roll their own copy. Every bridge
returns ``oracles.OracleResult`` directly — no more per-bridge Result dataclasses
or field-by-field copies in ``oracles._run_uncached``.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections.abc import Callable
from dataclasses import KW_ONLY, dataclass
from typing import TypeVar

from .provider_telemetry import ProviderTelemetry


@dataclass
class OracleResult:
    """The single normalized outcome type every bridge returns.

    ``key`` is set by the oracle dispatcher (not the bridge) — it identifies which
    oracle was asked (e.g. ``"fable"``, ``"minimax"``, ``"ollama:kimi-k2.7-code:cloud"``).
    ``session_id`` is Fable-SDK-only (for multi-turn resume). ``returncode`` is
    subprocess-bridge-only (for debugging non-zero exits).

    ``status`` is REQUIRED and the only positional field; everything after it is
    keyword-only so ``OracleResult("ok", some_text)`` is a ``TypeError`` instead of
    silently assigning ``some_text`` to ``key`` — the exact positional mistake a
    bridge author would otherwise make. A required status also means a bridge can't
    forget it and produce a result that reads as neither ok nor error."""

    status: str  # ok | refused | error — first positional, matching all callers
    _: KW_ONLY
    key: str = ""
    text: str = ""
    kind: str = ""
    model: str = ""
    thinking: str = ""
    session_id: str | None = None
    returncode: int | None = None
    telemetry: ProviderTelemetry | None = None

    def __post_init__(self) -> None:
        if self.status == "error" and self.telemetry is None:
            identity = self.key or self.model or "unknown"
            self.telemetry = ProviderTelemetry(
                oracle_key=identity,
                requested_model=self.model or None,
                actual_model=self.model or None,
                transport="error",
                returncode=self.returncode,
                wall_duration_ms=0.0,
                reasoning_available=False,
                usage_available=False,
                tools_available=False,
            )


def compose(question: str, context: str) -> str:
    """Frame a question + context into a single user message.

    Context (when non-empty) is labelled ``CODE CONTEXT:`` so the model treats it
    as the subject, not as instructions. Every bridge sends this as the user turn."""
    q = (question or "").strip()
    ctx = (context or "").strip()
    if ctx:
        return f"QUESTION:\n{q}\n\nCODE CONTEXT:\n{ctx}"
    return f"QUESTION:\n{q}"


def shape(text: str, session_id: str | None = None) -> OracleResult:
    """Map raw model text to an ``OracleResult``, honoring the ``REFUSED:`` scope
    contract shared by every oracle."""
    text = (text or "").strip()
    if not text:
        return OracleResult(
            "error", kind="sdk_error", text="empty response from model", session_id=session_id
        )
    if text.startswith("REFUSED:"):
        reason = text[len("REFUSED:") :].strip() or "off-scope"
        return OracleResult("refused", text=reason, session_id=session_id)
    return OracleResult("ok", text=text, session_id=session_id)


def timeout_default() -> float:
    """Per-turn wall-clock seconds (``ASK_FABLE_TIMEOUT``, default 240)."""
    try:
        return float(os.environ.get("ASK_FABLE_TIMEOUT") or 240.0)
    except (TypeError, ValueError):
        return 240.0


def gemini_timeout_default() -> float:
    """Like :func:`timeout_default` but prefers ``ASK_FABLE_GEMINI_TIMEOUT`` so the
    operator can cap the agentic ``agy`` CLI without lowering the global timeout."""
    for var in ("ASK_FABLE_GEMINI_TIMEOUT", "ASK_FABLE_TIMEOUT"):
        raw = os.environ.get(var)
        if raw:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    return 240.0


def codex_timeout_default() -> float:
    """Like :func:`timeout_default` but prefers ``ASK_FABLE_CODEX_TIMEOUT`` so the
    operator can cap the agentic ``codex`` CLI without lowering the global timeout."""
    for var in ("ASK_FABLE_CODEX_TIMEOUT", "ASK_FABLE_TIMEOUT"):
        raw = os.environ.get(var)
        if raw:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    return 240.0


def max_tokens() -> int:
    """Output token cap (``ASK_FABLE_MAX_TOKENS``, default 65536)."""
    try:
        return int(os.environ.get("ASK_FABLE_MAX_TOKENS") or 65536)
    except (TypeError, ValueError):
        return 65536


# HTTP statuses worth ONE retry: transient rate-limiting and gateway/server
# hiccups (529 is the Anthropic-style "overloaded" — the most transient of all).
# 4xx auth/scope failures and 400s (malformed request) are NOT here —
# retrying those just burns quota twice.
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504, 529})

# Constant backoff before the single transient retry, plus jitter at the call
# site so a fleet of councils doesn't retry in lockstep. No escalation — only
# one retry ever fires.
TRANSIENT_RETRY_BACKOFF_S = 2.0

# A retry fires only while at least this much budget would remain for the
# second attempt — a retry the deadline would kill anyway just converts the
# provider's real, actionable error into an opaque timeout.
_MIN_RETRY_WINDOW_S = 15.0
_MIN_RETRY_WINDOW_FRACTION = 0.25

_T = TypeVar("_T")


class TransientRetryTimeout(TimeoutError):
    """The total deadline expired inside :func:`call_with_transient_retry`.

    Carries the retry count and true elapsed wall time so callers report
    honest telemetry — a per-attempt "timed out after {timeout}s" message
    would understate real elapsed time when a retry preceded the timeout."""

    def __init__(self, retry_count: int, elapsed_s: float) -> None:
        super().__init__(f"timed out after {elapsed_s:.0f}s")
        self.retry_count = retry_count
        self.elapsed_s = elapsed_s


async def call_with_transient_retry(
    attempt: Callable[[], _T],
    *,
    timeout: float,
    retryable: Callable[[_T], bool],
) -> tuple[_T, int]:
    """Run blocking ``attempt`` in a thread with ONE retry when
    ``retryable(result)`` (typically: the HTTP status landed in
    :data:`RETRYABLE_HTTP_STATUSES`).

    Both attempts share a single total deadline of ``timeout + 5`` seconds.
    The council/chain wall-clock caps in server.py assume a backend's wall
    time is bounded by its own inner timeout; a fresh window per attempt
    would break that and get the panelist cancelled at the cap — masking the
    provider's real error behind ``kind="timeout"``. For the same reason the
    retry fires only while enough budget remains for a useful second attempt;
    otherwise the first attempt's real error is returned immediately.

    Returns ``(result, retry_count)``. Raises :class:`TransientRetryTimeout`
    when the deadline expires mid-attempt."""
    started = time.monotonic()
    deadline = started + timeout + 5
    retry_count = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransientRetryTimeout(retry_count, time.monotonic() - started)
        try:
            result = await asyncio.wait_for(asyncio.to_thread(attempt), remaining)
        except TimeoutError:
            raise TransientRetryTimeout(retry_count, time.monotonic() - started) from None
        if retry_count < 1 and retryable(result):
            backoff = TRANSIENT_RETRY_BACKOFF_S + random.random()
            window = max(_MIN_RETRY_WINDOW_S, timeout * _MIN_RETRY_WINDOW_FRACTION)
            if deadline - time.monotonic() - backoff >= window:
                retry_count += 1
                await asyncio.sleep(backoff)
                continue
        return result, retry_count


def parse_anthropic_envelope(obj: dict) -> tuple[str | None, str, str | None]:
    """Extract ``(text, thinking, error)`` from an Anthropic-Messages-style response
    object. Shared by the MiniMax ``mmx`` bridge and the HTTP providers (GLM, DeepSeek).

    On success: ``text`` is the joined text blocks, ``thinking`` the joined thinking
    blocks, ``error`` is None. On failure: ``text`` is None, ``error`` carries the
    reason."""
    # An `error` KEY alone is not an error — some gateways include `"error": null`
    # on success envelopes. Only a dict/non-empty-string value (or type=error) is.
    err_obj = obj.get("error")
    if obj.get("type") == "error" or isinstance(err_obj, dict):
        err = err_obj if isinstance(err_obj, dict) else {}
        return None, "", f"{err.get('type', 'error')}: {err.get('message', obj)}"
    if isinstance(err_obj, str) and err_obj.strip():
        return None, "", f"error: {err_obj.strip()}"
    blocks = obj.get("content")
    if not isinstance(blocks, list):
        return None, "", "unrecognized response shape (no content[])"
    texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    thinks = [
        b.get("thinking", "") for b in blocks if isinstance(b, dict) and b.get("type") == "thinking"
    ]
    return "".join(texts).strip(), "\n".join(t for t in thinks if t).strip(), None


def parse_anthropic_body(body: bytes) -> tuple[str | None, str, str | None]:
    """Decode + parse an Anthropic response body. Returns ``(text, thinking, error)``."""
    try:
        obj = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return None, "", f"unparseable response: {exc}"
    return parse_anthropic_envelope(obj)


def extract_http_error(e: Exception) -> str:
    """Extract a short error detail from an ``HTTPError`` (reads the body) or
    ``URLError``. Shared by the HTTP bridges (``anthropic_http``, ``ollama``)."""
    import urllib.error

    if isinstance(e, urllib.error.HTTPError):
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return f"HTTP {e.code}: {detail or e.reason}"
    if isinstance(e, urllib.error.URLError):
        return f"network error: {e.reason}"
    return f"{type(e).__name__}: {e}"


def _clip(text: str, n: int = 300) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def _auth_phrasing(lowered: str) -> bool:
    """Auth-failure vocabulary shared by the CLI and HTTP error classifiers.

    A bad key / logged-out CLI is a CONFIG state (``kind="auth_failed"``),
    which health.py exempts from the circuit breaker so the actionable
    "fix your key / run /login" message keeps surfacing instead of being
    hidden behind ``circuit_open``. One shared vocabulary keeps the two
    transports classifying identically — auth was previously HTTP-only and
    CLI auth failures still tripped the breaker."""
    return (
        "http 401" in lowered
        or "http 403" in lowered
        or "invalid api key" in lowered
        or "invalid x-api-key" in lowered
        or "unauthorized" in lowered
        or "unauthenticated" in lowered
        or "authentication" in lowered
        or "please run /login" in lowered
        or "not logged in" in lowered
        or "logged out" in lowered
    )


def _model_unavailable_phrasing(lowered: str) -> bool:
    """Vocabulary for "this transport cannot run the model that was asked for".

    Shared by the CLI and HTTP classifiers so both produce ``model_unavailable``.
    Like ``auth_failed`` this is a LOCAL state, not backend ill-health: the usual
    cause is a Claude Code build older than the requested model (the Agent SDK
    ships its own bundled binary, which can lag the one on PATH by months), so
    health.py exempts it from the circuit breaker and ``fable.run`` uses it to
    step down its model ladder instead of failing the turn."""
    return (
        "model_not_found" in lowered
        or "unrecognized_model" in lowered
        or "does not support this model" in lowered
        or "issue with the selected model" in lowered
        or "model catalog" in lowered
        # Gateway phrasing for an id that is not in their catalog (OpenRouter:
        # "<id> is not a valid model ID"). A mistyped model is the CALLER's
        # mistake, not the provider being unwell, so it must not push the
        # circuit breaker toward open and take the whole backend down with it.
        or "not a valid model" in lowered
        or "invalid model" in lowered
        or "unknown model" in lowered
    )


def cli_error_detail(
    *,
    label: str,
    returncode: int | None,
    stderr: str = "",
    stdout: str = "",
) -> tuple[str, str]:
    """Turn a failed CLI subprocess into ``(kind, detail)`` for OracleResult.

    Prefers structured JSON on stderr (``mmx`` emits ``{"error":{message,code}}``),
    then plain stderr, then stdout. Classifies auth failures as ``auth_failed``
    (same vocabulary as the HTTP bridges) and quota/rate-limit failures as
    ``rate_limit`` so audit/hub/circuit-breaker consumers can act without opening
    a trace bundle. Detail is clipped and never empty.
    """
    raw = (stderr or "").strip() or (stdout or "").strip()
    message = ""
    code: int | str | None = None
    if raw:
        # Try JSON envelope anywhere in the first non-empty line / whole blob.
        blob = raw
        try:
            obj = json.loads(blob)
        except (ValueError, TypeError):
            # Sometimes a banner precedes JSON — scan for first `{…}`.
            start = blob.find("{")
            end = blob.rfind("}")
            obj = None
            if start >= 0 and end > start:
                try:
                    obj = json.loads(blob[start : end + 1])
                except (ValueError, TypeError):
                    obj = None
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                message = str(err.get("message") or err.get("msg") or "").strip()
                code = err.get("code")
                hint = str(err.get("hint") or "").strip()
                if hint and message and hint not in message:
                    message = f"{message} ({hint})"
            elif isinstance(err, str) and err.strip():
                message = err.strip()
            elif obj.get("message"):
                message = str(obj.get("message")).strip()
                code = obj.get("code", code)
        if not message:
            # First non-empty line of plain stderr, stripped of ANSI-ish noise.
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    message = line
                    break

    lowered = message.lower()
    authish = _auth_phrasing(lowered) or code in (401, "401")
    rateish = (
        "rate limit" in lowered
        or "quota" in lowered
        or "usage limit" in lowered
        or "too many requests" in lowered
        or code in (429, "429")
        # mmx's quota errors carry code 4 (observed CLI behavior). Other CLIs' code 4
        # is not a rate-limit signal (gRPC 4 is DEADLINE_EXCEEDED), so scope it.
        or (code in (4, "4") and label.lower().startswith("minimax"))
    )
    kind = (
        "auth_failed" if authish
        else "model_unavailable" if _model_unavailable_phrasing(lowered)
        else "rate_limit" if rateish
        else "sdk_error"
    )
    if message:
        detail = _clip(message)
    elif returncode is not None:
        detail = f"{label} CLI failed (exit {returncode})"
    else:
        detail = f"{label} CLI failed"
    return kind, detail


def http_error_detail(
    *,
    label: str,
    error: str,
    http_status: int | None = None,
) -> tuple[str, str]:
    """Turn a failed HTTP-bridge call into ``(kind, text)`` — the HTTP sibling of
    :func:`cli_error_detail`. A 429 (or rate-limit/quota phrasing in the error body)
    classifies as ``rate_limit`` so audit/hub/circuit-breaker consumers see the same
    vocabulary as the CLI bridges. A 401/403 (or auth phrasing) classifies as
    ``auth_failed`` — a bad key is a CONFIG state, not a generic API error, and
    callers/breakers must be able to tell it apart from ``sdk_error``. The detail
    is clipped, never dropped — a bad key or exhausted quota must be readable
    without opening a trace bundle."""
    lowered = (error or "").lower()
    authish = http_status in (401, 403) or _auth_phrasing(lowered)
    rateish = (
        http_status == 429
        or "http 429" in lowered
        or "rate limit" in lowered
        or "rate_limit" in lowered
        or "quota" in lowered
        or "usage limit" in lowered
        or "too many requests" in lowered
    )
    kind = (
        "auth_failed" if authish
        else "model_unavailable" if _model_unavailable_phrasing(lowered)
        else "rate_limit" if rateish
        else "sdk_error"
    )
    detail = _clip(error)
    text = f"{label} request failed: {detail}" if detail else f"{label} request failed"
    return kind, text
