"""Shared helpers for every oracle bridge — message framing, REFUSED shaping,
timeout/token defaults, and Anthropic-envelope parsing.

Centralized so the five bridge modules (``fable``, ``minimax``, ``gemini``,
``anthropic_http``, ``ollama``) don't each roll their own copy. Every bridge
returns ``oracles.OracleResult`` directly — no more per-bridge Result dataclasses
or field-by-field copies in ``oracles._run_uncached``.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import random
import ssl
import time
from collections.abc import Callable
from dataclasses import KW_ONLY, dataclass, field
from typing import TypeVar

from .cli_gate import ARGV_REJECTED_PREFIX
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
    # Extra bridge-specific facts (e.g. LM Studio's load/swap confirmation) that
    # the single-oracle handler merges into its payload without every oracle
    # having to grow a field. Never allowed to overwrite the core payload keys.
    meta: dict = field(default_factory=dict)

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


# Stop/finish reasons meaning the answer was CUT OFF rather than finished: the
# OpenAI-style `finish_reason` and Ollama's `done_reason` say "length", the
# Anthropic-style `stop_reason` says "max_tokens" (or, when the context window
# rather than the output cap ran out, "model_context_window_exceeded").
CAPPED_STOP_REASONS = frozenset({"length", "max_tokens", "model_context_window_exceeded"})


def shape_stopped(
    text: str | None, stop_reason: str | None, *, label: str, thinking: str = ""
) -> OracleResult:
    """:func:`shape`, told why generation stopped (the provider's stop reason).

    An answer the output cap cut off is still returned — half an answer beats
    none — but flagged ``kind="truncated"`` (+ ``meta.partial``), which every
    cache layer refuses to pin: served for an hour as complete, a cut-off answer
    is a lie. A cap hit with NO answer is a reasoning model that spent the whole
    budget thinking. That is the request's budget, not the backend's health, so
    it is ``budget_exhausted`` (a non-health kind) instead of the ``sdk_error``
    "empty response" that used to push the circuit breaker open."""
    res = shape(text or "")
    reason = str(stop_reason or "").strip().lower()
    if reason not in CAPPED_STOP_REASONS:
        return res
    if res.status == "ok":
        res.kind = "truncated"
        res.meta = {"partial": True, "stop_reason": reason}
    elif res.status == "error":  # shape() errors only on an empty answer
        spent = "reasoning " if thinking else ""
        res = OracleResult(
            "error",
            kind="budget_exhausted",
            text=(
                f"{label} spent its whole output-token budget {spent}and returned no "
                f"answer (stop reason {reason!r}) — raise the effort or the token cap, "
                "or ask a narrower question"
            ),
        )
    return res


# The PROVIDER's own safeguard (Anthropic's classifier, which reads the WHOLE
# payload — `context` included) is a refusal that happens UPSTREAM of our guard.
# The SDK reports it as an error (`is_error` + `stop_reason="refusal"` and a prose
# sentence); the CLI/HTTP bridges only ever see the sentence. Both must map to our
# `refused` status rather than a generic `sdk_error` — otherwise the refusal is
# invisible to `stats` (bucketed as an error), counts against the circuit breaker
# (refusals don't trip it, but errors do), and reaches the caller as an opaque
# error with no reframe recipe.
_PROVIDER_REFUSAL_PHRASES = (
    "flagged this message",
    "cyber verification program",
)


def is_provider_refusal(stop_reason: str | None, message: str = "") -> str | None:
    """Short reason when this looks like a PROVIDER-safeguard refusal, else None.

    Distinct from our guard's deterministic refusal: this one is NOT
    deterministic — the same payload may pass on another backend. That is why
    the handler attaches a different recipe to it."""
    if (stop_reason or "").strip().lower() == "refusal":
        return "provider safeguard refusal"
    lowered = (message or "").lower()
    if any(phrase in lowered for phrase in _PROVIDER_REFUSAL_PHRASES):
        return "provider safeguard refusal"
    return None


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


# The CLI bridges that pass the prompt as an argv VALUE — codex, gemini, grok and
# kimi; none of those CLIs has a stdin prompt mode — are capped by the kernel, not
# the model: Linux limits ONE argv element to MAX_ARG_STRLEN = 32 pages = 131072
# bytes (131071 usable), and `Popen` fails with OSError(E2BIG) above it. That
# spawn failure used to surface as an `sdk_error`, so a few big-context calls
# opened the circuit breaker for everyone; the bridges now refuse up front with
# `context_too_large` — a property of the request, which health.py keeps out of
# the breaker.
MAX_ARGV_PROMPT_BYTES = 120_000  # margin under 131071 for the rest of argv


def argv_prompt_fits(prompt: str) -> bool:
    """True when ``prompt`` fits in ONE argv value (:data:`MAX_ARGV_PROMPT_BYTES`)."""
    return len(prompt.encode("utf-8", "replace")) <= MAX_ARGV_PROMPT_BYTES


def argv_too_large(prompt: str, *, binary: str, advice: str) -> str | None:
    """The ``context_too_large`` detail when ``prompt`` cannot ride as ONE argv
    value to the local ``binary`` CLI, else None. ``advice`` names a way around it."""
    if argv_prompt_fits(prompt):
        return None
    size = len(prompt.encode("utf-8", "replace"))
    return (
        f"prompt is {size} bytes; the local `{binary}` CLI takes it as a single argv "
        "value, which the kernel caps at ~131k (MAX_ARG_STRLEN), so ask_fable rejects "
        f"above {MAX_ARGV_PROMPT_BYTES}. {advice}"
    )


def max_tokens() -> int:
    """Output token cap (``ASK_FABLE_MAX_TOKENS``, default 65536)."""
    try:
        return int(os.environ.get("ASK_FABLE_MAX_TOKENS") or 65536)
    except (TypeError, ValueError):
        return 65536


def websearch_max_turns() -> int:
    """Agentic turn budget for the opt-in ``ask_websearch`` search loop
    (``ASK_FABLE_WEBSEARCH_MAX_TURNS``, default 20).

    A pure-reasoning oracle turn is ``max_turns=1``; a web-search turn is an
    agentic loop — search → (optional fetch) → answer — so it needs several. Kept
    here beside the other per-turn defaults so the grok and Claude search paths
    read ONE knob instead of each rolling its own (and neither has to import the
    ``websearch`` router, which imports them)."""
    try:
        return max(2, int(os.environ.get("ASK_FABLE_WEBSEARCH_MAX_TURNS") or 20))
    except (TypeError, ValueError):
        return 20


# HTTP statuses worth ONE retry: transient rate-limiting and gateway/server
# hiccups (529 is the Anthropic-style "overloaded" — the most transient of all).
# 4xx auth/scope failures and 400s (malformed request) are NOT here —
# retrying those just burns quota twice.
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504, 529})

# Transport failures urllib does NOT wrap in URLError. It wraps only what SENDING
# the request raises; everything after that — reading the status line (a server
# or proxy that closes without one: RemoteDisconnected), reading the body
# (IncompleteRead), a reset, a TLS EOF — escapes `urlopen`/`read()` raw. An HTTP
# bridge that catches only HTTPError/URLError therefore RAISES on a dropped
# connection, which aborts a whole chain and skips a council's fallbacks. They are
# as transient as a 502, so the bridges report them as a network error and give
# them the same one bounded retry. TimeoutError is deliberately absent (it is
# neither a ConnectionError nor an SSLError): it must still reach the retry
# helper's deadline and come back as kind="timeout".
DROPPED_CONNECTION_ERRORS: tuple[type[Exception], ...] = (
    http.client.HTTPException,
    ConnectionError,
    ssl.SSLError,
)

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
    """Extract a short error detail from an ``HTTPError`` (reads the body), a
    ``URLError`` or a dropped connection (:data:`DROPPED_CONNECTION_ERRORS`).
    Shared by the HTTP bridges."""
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
    if isinstance(e, DROPPED_CONNECTION_ERRORS):
        return f"network error: {type(e).__name__}: {e}"
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
    if returncode is None and raw.startswith(ARGV_REJECTED_PREFIX):
        # The prompt itself could not be passed (NUL byte / unpaired surrogate): the
        # caller's input, never a sign the backend is unhealthy (non-health kind).
        return "bad_input", _clip(raw)
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
    without opening a trace bundle.

    Two statuses are NOT what they look like. A 403 whose body says the input was
    flagged by moderation (OpenRouter: "<model> requires moderation … flagged for
    …") is the provider refusing THIS payload — ``provider_refusal``, which a
    bridge reports as ``refused`` (see :func:`error_status`), not a bad key whose
    remedy would be wrong. A 402 is an account out of credits —
    ``payment_required``, chronic until the operator pays, so like
    ``auth_failed`` it must not feed the circuit breaker."""
    lowered = (error or "").lower()
    moderated = (http_status == 403 or "http 403" in lowered) and (
        "moderation" in lowered or "flagged" in lowered
    )
    unpaid = http_status == 402 or "http 402" in lowered
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
        "provider_refusal" if moderated
        else "payment_required" if unpaid
        else "auth_failed" if authish
        else "model_unavailable" if _model_unavailable_phrasing(lowered)
        else "rate_limit" if rateish
        else "sdk_error"
    )
    detail = _clip(error)
    if kind == "provider_refusal":
        return kind, f"provider safeguard refusal ({label}): {detail}"
    text = f"{label} request failed: {detail}" if detail else f"{label} request failed"
    return kind, text


def error_status(kind: str) -> str:
    """The ``OracleResult`` status for a failure :func:`http_error_detail` classified.

    A provider-safeguard refusal is ``refused``, not ``error``: a refusal never
    counts against the circuit breaker, and the handler attaches the reframe
    recipe to it (``kind == "provider_refusal"``) instead of an error payload."""
    return "refused" if kind == "provider_refusal" else "error"
