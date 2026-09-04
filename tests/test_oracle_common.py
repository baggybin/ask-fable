"""Tests for oracle_common — the shared helpers every bridge depends on."""

import json
import urllib.error

import pytest

from ask_fable import oracle_common as oc

# --- compose ---------------------------------------------------------------

def test_compose_with_context_labels_both_sections():
    msg = oc.compose("why is this slow?", "def f(): ...")
    assert msg == "QUESTION:\nwhy is this slow?\n\nCODE CONTEXT:\ndef f(): ..."


def test_compose_without_context_omits_context_section():
    msg = oc.compose("  question  ", "   ")
    assert msg == "QUESTION:\nquestion"
    assert "CODE CONTEXT" not in msg


def test_compose_handles_none():
    assert oc.compose(None, None) == "QUESTION:\n"


# --- shape -----------------------------------------------------------------

def test_shape_ok():
    r = oc.shape("an answer", session_id="s1")
    assert (r.status, r.text, r.session_id) == ("ok", "an answer", "s1")


def test_shape_empty_is_error():
    r = oc.shape("   ")
    assert r.status == "error"
    assert r.kind == "sdk_error"


def test_shape_refused_extracts_reason():
    r = oc.shape("REFUSED: off-scope biology question")
    assert r.status == "refused"
    assert r.text == "off-scope biology question"


def test_shape_refused_with_no_reason_gets_default():
    r = oc.shape("REFUSED:")
    assert r.status == "refused"
    assert r.text == "off-scope"


def test_shape_refused_only_matches_prefix():
    # REFUSED: mid-text must not be classified as a refusal.
    r = oc.shape("The API returned REFUSED: but that's just the payload text.")
    assert r.status == "ok"


# --- OracleResult contract ---------------------------------------------------

def test_oracle_result_rejects_second_positional():
    with pytest.raises(TypeError):
        oc.OracleResult("ok", "text-must-be-keyword")  # noqa: B026


# --- env-tunable defaults ----------------------------------------------------

def test_timeout_default_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_TIMEOUT", "not-a-number")
    assert oc.timeout_default() == 240.0


def test_gemini_timeout_prefers_gemini_var(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_TIMEOUT", "60")
    monkeypatch.setenv("ASK_FABLE_GEMINI_TIMEOUT", "300")
    assert oc.gemini_timeout_default() == 300.0
    assert oc.timeout_default() == 60.0


def test_gemini_timeout_skips_garbage_and_uses_global(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_GEMINI_TIMEOUT", "soon")
    monkeypatch.setenv("ASK_FABLE_TIMEOUT", "45")
    assert oc.gemini_timeout_default() == 45.0


def test_max_tokens_default(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_MAX_TOKENS", raising=False)
    assert oc.max_tokens() == 65536


# --- Anthropic envelope parsing ----------------------------------------------

def test_parse_envelope_joins_text_and_thinking_blocks():
    obj = {
        "content": [
            {"type": "thinking", "thinking": "step 1"},
            {"type": "text", "text": "hello "},
            {"type": "thinking", "thinking": "step 2"},
            {"type": "text", "text": "world"},
        ]
    }
    text, thinking, err = oc.parse_anthropic_envelope(obj)
    assert err is None
    assert text == "hello world"
    assert thinking == "step 1\nstep 2"


def test_parse_envelope_error_object():
    obj = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    text, thinking, err = oc.parse_anthropic_envelope(obj)
    assert text is None
    assert err == "overloaded_error: busy"


def test_parse_envelope_no_content_is_error():
    text, _, err = oc.parse_anthropic_envelope({"id": "msg_x"})
    assert text is None
    assert "no content[]" in err


def test_parse_envelope_ignores_malformed_blocks():
    obj = {"content": [{"type": "text", "text": "ok"}, "not-a-dict", {"type": "tool_use"}]}
    text, thinking, err = oc.parse_anthropic_envelope(obj)
    assert (text, thinking, err) == ("ok", "", None)


def test_parse_body_unparseable_bytes():
    text, _, err = oc.parse_anthropic_body(b"\xff\xfe not json")
    assert text is None
    assert "unparseable response" in err


def test_parse_body_roundtrip():
    body = json.dumps({"content": [{"type": "text", "text": "hi"}]}).encode()
    assert oc.parse_anthropic_body(body) == ("hi", "", None)


# --- extract_http_error --------------------------------------------------------

class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body, reason="reason"):
        # HTTPError needs a file-like; emulate the minimal read() surface.
        self._body = body
        super().__init__("http://x", code, reason, hdrs=None, fp=None)

    def read(self):  # HTTPError.read() comes from the fp normally
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_extract_http_error_includes_truncated_body():
    e = _FakeHTTPError(429, b"rate limited " * 100)
    msg = oc.extract_http_error(e)
    assert msg.startswith("HTTP 429: ")
    assert len(msg) <= len("HTTP 429: ") + 300


def test_extract_http_error_falls_back_to_reason_when_body_unreadable():
    e = _FakeHTTPError(500, OSError("gone"), reason="Server Error")
    assert oc.extract_http_error(e) == "HTTP 500: Server Error"


def test_extract_http_error_urlerror():
    assert oc.extract_http_error(urllib.error.URLError("conn refused")) == "network error: conn refused"


def test_extract_http_error_generic_exception():
    assert oc.extract_http_error(ValueError("boom")) == "ValueError: boom"


# --- call_with_transient_retry ---------------------------------------------


def test_transient_retry_timeout_carries_retry_state(monkeypatch):
    """When the shared deadline expires, the raised error carries the retry
    count and true elapsed time so callers report honest telemetry."""
    import asyncio

    async def fake_wait_for(aw, timeout):
        aw.close()  # never awaited — avoid a pending-coroutine warning
        raise TimeoutError

    monkeypatch.setattr(oc.asyncio, "wait_for", fake_wait_for)

    async def go():
        await oc.call_with_transient_retry(lambda: None, timeout=1, retryable=lambda r: False)

    with pytest.raises(oc.TransientRetryTimeout) as ei:
        asyncio.run(go())
    assert ei.value.retry_count == 0
    assert "timed out after" in str(ei.value)


def test_transient_retry_total_budget_is_timeout_plus_five(monkeypatch):
    """Each attempt runs under the REMAINING budget, not a fresh timeout+5
    window — the council/chain caps assume a backend's wall time is bounded
    by its own inner timeout."""
    import asyncio

    windows = []

    async def fake_wait_for(aw, timeout):
        windows.append(timeout)
        return await aw

    monkeypatch.setattr(oc.asyncio, "wait_for", fake_wait_for)

    async def _no_sleep(s):  # noqa: ANN001
        pass

    monkeypatch.setattr(oc.asyncio, "sleep", _no_sleep)

    async def go():
        return await oc.call_with_transient_retry(
            lambda: "err", timeout=240, retryable=lambda r: r == "err"
        )

    result, retries = asyncio.run(go())
    assert retries == 1
    assert len(windows) == 2
    assert windows[0] <= 245  # first attempt: the full budget
    assert windows[1] < windows[0]  # second attempt: only what remains
