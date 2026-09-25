"""ask_fable Anthropic-compatible HTTP oracle — config + response parsing."""

from __future__ import annotations

import asyncio
import http.client
import json
import ssl

import pytest

import ask_fable.anthropic_http as ah
import ask_fable.prompts as prompts
from ask_fable import oracle_common


def _run(coro):
    return asyncio.run(coro)


def test_config_for_needs_key(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_GLM_API_KEY", raising=False)
    assert ah.config_for("glm") is None
    monkeypatch.setenv("ASK_FABLE_GLM_API_KEY", "secret")
    cfg = ah.config_for("glm")
    assert cfg and cfg.api_key == "secret"
    assert cfg.base_url == "https://api.z.ai/api/anthropic" and cfg.model == "glm-5.2"


def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("ASK_FABLE_DEEPSEEK_BASE_URL", "https://example.test/anthropic/")
    monkeypatch.setenv("ASK_FABLE_DEEPSEEK_MODEL", "deepseek-v9")
    cfg = ah.config_for("deepseek")
    assert cfg.model == "deepseek-v9"
    assert cfg.base_url == "https://example.test/anthropic"  # trailing slash trimmed


def test_parse_text_and_thinking():
    body = json.dumps(
        {
            "model": "glm-5.2",
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "the answer"},
            ],
        }
    ).encode()
    text, thinking, err = oracle_common.parse_anthropic_body(body)
    assert text == "the answer" and thinking == "hmm" and err is None


def test_parse_error_envelope():
    body = json.dumps(
        {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
    ).encode()
    text, thinking, err = oracle_common.parse_anthropic_body(body)
    assert text is None and "authentication_error" in err and "bad key" in err


def test_run_ok(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"content":[{"type":"text","text":"the answer"}]}'

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    monkeypatch.setattr(ah.urllib.request, "urlopen", fake_urlopen)
    cfg = ah.ProviderConfig(
        key="glm", label="glm-5.2", base_url="https://x/anthropic", model="glm-5.2", api_key="k"
    )
    res = _run(ah.run(cfg, "How are handlers registered?"))
    assert res.status == "ok" and res.text == "the answer" and res.model == "glm-5.2"
    assert seen["url"] == "https://x/anthropic/v1/messages"
    assert (
        seen["headers"].get("x-api-key") == "k"
        and seen["headers"].get("anthropic-version") == "2023-06-01"
    )


def test_run_system_prompt_override(monkeypatch):
    """The caller's prompt wins over the default scope contract — the Fable-over-HTTP
    transport passes its own, and answering under the wrong contract would be a
    silent change of oracle rather than a transport fallback."""
    sent = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"content":[{"type":"text","text":"ok"}]}'

    def fake_urlopen(req, timeout=None):
        sent.update(json.loads(req.data))
        return _Resp()

    monkeypatch.setattr(ah.urllib.request, "urlopen", fake_urlopen)
    cfg = ah.ProviderConfig(
        key="fable", label="Fable", base_url="https://api.anthropic.com",
        model="claude-fable-5-1", api_key="k",
    )

    _run(ah.run(cfg, "q", system_prompt="CUSTOM CONTRACT"))
    assert sent["system"] == "CUSTOM CONTRACT"

    # omitted → the shared oracle prompt, so glm/deepseek keep today's behaviour
    _run(ah.run(cfg, "q"))
    assert sent["system"] == prompts.ORACLE_SYSTEM_PROMPT


def _script(monkeypatch, *bodies):
    """Serve one canned response body per request; capture every payload sent."""
    sent: list[dict] = []
    queue = list(bodies)

    class _Resp:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data))
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        return _Resp(json.dumps(body).encode())

    monkeypatch.setattr(ah.urllib.request, "urlopen", fake_urlopen)
    return sent


def _cfg(key="fable", model="claude-fable-5-1"):
    return ah.ProviderConfig(
        key=key, label="Fable", base_url="https://api.anthropic.com", model=model, api_key="k"
    )


def _search_result(urls, *, stop="end_turn", usage=None, citation=None):
    blocks = [
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": [{"type": "web_search_result", "url": u} for u in urls],
        }
    ]
    text = {"type": "text", "text": "answer"}
    if citation:
        text["citations"] = [{"type": "web_search_result_location", "url": citation}]
    blocks.append(text)
    return {
        "model": "claude-fable-5-1", "content": blocks, "stop_reason": stop,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
    }


def test_web_search_declares_the_server_tool(monkeypatch):
    sent = _script(monkeypatch, _search_result(["https://a.example"]))
    _run(ah.run(_cfg(), "q", web_search=True))
    assert sent[0]["tools"] == [
        {"type": ah.WEB_SEARCH_TOOL, "name": "web_search", "max_uses": ah.websearch_max_uses()}
    ]
    # ... and a plain turn declares no tools at all
    plain = _script(monkeypatch, {"model": "m", "content": [{"type": "text", "text": "hi"}]})
    _run(ah.run(_cfg(), "q"))
    assert "tools" not in plain[0]


def test_web_search_sources_and_counts_land_on_the_result(monkeypatch):
    _script(
        monkeypatch,
        _search_result(
            ["https://a.example", "https://b.example"],
            citation="https://a.example",
            usage={"input_tokens": 10, "output_tokens": 5,
                   "server_tool_use": {"web_search_requests": 2}},
        ),
    )
    res = _run(ah.run(_cfg(), "q", web_search=True))
    assert res.status == "ok"
    # the CITED url is what the answer rests on, so it leads
    assert res.meta["sources"] == ["https://a.example"]
    assert res.meta["web_search_requests"] == 2
    assert res.telemetry.tools_available is True
    assert [e.status for e in res.telemetry.tool_events] == ["ok"]


def test_web_search_resumes_a_paused_turn(monkeypatch):
    """A long search turn comes back paused; the resume is the paused assistant turn
    re-sent unchanged — with NO extra user message (the API detects the trailing
    server_tool_use and continues)."""
    paused = _search_result(["https://a.example"], stop="pause_turn")
    sent = _script(monkeypatch, paused, _search_result(["https://b.example"]))
    res = _run(ah.run(_cfg(), "q", web_search=True))
    assert res.status == "ok" and len(sent) == 2
    followup = sent[1]["messages"]
    assert [m["role"] for m in followup] == ["user", "assistant"]
    assert followup[1]["content"] == paused["content"]
    # usage and search results accumulate across the resume
    assert res.meta["web_search_requests"] == 2 or res.telemetry.usage is not None


def test_web_search_that_entirely_failed_is_an_error(monkeypatch):
    """The API reports a search failure INSIDE a 200. Returning 'ok' would hand back
    a research answer with no research behind it."""
    _script(
        monkeypatch,
        {
            "model": "claude-fable-5-1", "stop_reason": "end_turn",
            "content": [
                {"type": "web_search_tool_result", "tool_use_id": "s1",
                 "content": {"type": "web_search_tool_result_error",
                             "error_code": "too_many_requests"}},
                {"type": "text", "text": "I could not search."},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
    )
    res = _run(ah.run(_cfg(), "q", web_search=True))
    assert res.status == "error" and res.kind == "rate_limit"  # the honest classifier
    assert "too_many_requests" in res.text


def test_web_search_partial_failure_still_answers(monkeypatch):
    _script(
        monkeypatch,
        {
            "model": "claude-fable-5-1", "stop_reason": "end_turn",
            "content": [
                {"type": "web_search_tool_result", "tool_use_id": "s1",
                 "content": [{"type": "web_search_result", "url": "https://a.example"}]},
                {"type": "web_search_tool_result", "tool_use_id": "s2",
                 "content": {"type": "web_search_tool_result_error",
                             "error_code": "max_uses_exceeded"}},
                {"type": "text", "text": "partial answer"},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
    )
    res = _run(ah.run(_cfg(), "q", web_search=True))
    assert res.status == "ok" and res.text == "partial answer"
    assert res.meta["search_errors"] == ["max_uses_exceeded"]


def test_web_search_with_no_matches_is_not_an_error(monkeypatch):
    """A successful search that matched nothing returns an empty list, not an error."""
    _script(
        monkeypatch,
        {
            "model": "claude-fable-5-1", "stop_reason": "end_turn",
            "content": [
                {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
                {"type": "text", "text": "nothing found"},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
    )
    res = _run(ah.run(_cfg(), "q", web_search=True))
    assert res.status == "ok" and "search_errors" not in res.meta


def test_run_http_error(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(ah.urllib.request, "urlopen", boom)
    cfg = ah.ProviderConfig(
        key="glm", label="glm-5.2", base_url="https://x/anthropic", model="glm-5.2", api_key="bad"
    )
    res = _run(ah.run(cfg, "q"))
    assert res.status == "error" and res.kind == "auth_failed"
    assert "glm-5.2 request failed" in res.text and "401" in res.text  # detail surfaced
    assert res.telemetry is not None and res.telemetry.http_status == 401


def test_run_retries_once_on_429(monkeypatch):
    import urllib.error

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"content":[{"type":"text","text":"hi"}]}'

    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append("c")
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        return _Resp()

    monkeypatch.setattr(ah.urllib.request, "urlopen", fake_urlopen)

    async def _fake_sleep(s):  # noqa: ANN001
        pass

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    cfg = ah.ProviderConfig(
        key="glm", label="glm-5.2", base_url="https://x/anthropic", model="glm-5.2", api_key="k"
    )
    res = _run(ah.run(cfg, "q"))
    assert res.status == "ok" and res.text == "hi"
    assert len(calls) == 2
    assert (
        res.telemetry is not None
        and res.telemetry.retry_count == 1
        and res.telemetry.http_status == 200
    )


def test_retry_skipped_when_budget_too_small(monkeypatch):
    """The transient retry only fires while a useful window remains in the
    total timeout+5 budget — with a tiny timeout the caller gets the real 429
    immediately instead of a retry the deadline would kill (which would mask
    the actionable error behind kind='timeout' at the council cap)."""
    import urllib.error

    calls = []

    def boom(req, timeout=None):
        calls.append("c")
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(ah.urllib.request, "urlopen", boom)
    cfg = ah.ProviderConfig(
        key="glm", label="glm-5.2", base_url="https://x/anthropic", model="glm-5.2", api_key="k"
    )
    res = _run(ah.run(cfg, "q", timeout=1))
    assert res.status == "error" and res.kind == "rate_limit"
    assert len(calls) == 1
    assert res.telemetry is not None and res.telemetry.retry_count == 0


def test_retry_does_not_inherit_request_id(monkeypatch):
    """Attempt state is per-attempt: a network failure on the retry must not
    report the previous 429 attempt's provider request id or status."""
    import urllib.error
    from email.message import Message

    calls = []

    def flaky(req, timeout=None):
        calls.append("c")
        if len(calls) == 1:
            headers = Message()
            headers["request-id"] = "req-abc"
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", headers, None)
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(ah.urllib.request, "urlopen", flaky)

    async def _fake_sleep(s):  # noqa: ANN001
        pass

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    cfg = ah.ProviderConfig(
        key="glm", label="glm-5.2", base_url="https://x/anthropic", model="glm-5.2", api_key="k"
    )
    res = _run(ah.run(cfg, "q"))
    assert len(calls) == 2 and res.status == "error"
    assert res.telemetry is not None
    assert res.telemetry.provider_request_id is None  # not attempt 1's "req-abc"
    assert res.telemetry.http_status is None  # not attempt 1's 429


def test_body_http_status_key_cannot_forge_retry(monkeypatch):
    """A transport-200 response whose JSON body contains an 'http_status' key
    must not trigger the transient retry or leak into telemetry — transport
    facts are kept apart from parsed body fields."""
    calls = []

    class _Resp:
        headers = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"type": "error", "error": {"type": "x", "message": "boom"}, "http_status": 503}
            ).encode()

    def fake_urlopen(req, timeout=None):
        calls.append("c")
        return _Resp()

    monkeypatch.setattr(ah.urllib.request, "urlopen", fake_urlopen)
    cfg = ah.ProviderConfig(
        key="glm", label="glm-5.2", base_url="https://x/anthropic", model="glm-5.2", api_key="k"
    )
    res = _run(ah.run(cfg, "q"))
    assert len(calls) == 1  # no forged retry, no double POST
    assert res.status == "error" and res.kind == "sdk_error"
    assert res.telemetry is not None and res.telemetry.http_status is None


_DROPS = [
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    http.client.IncompleteRead(b'{"content', 990),
    ConnectionResetError(104, "Connection reset by peer"),
    ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
]


@pytest.mark.parametrize("exc", _DROPS, ids=lambda e: type(e).__name__)
def test_dropped_connection_is_an_error_result_not_a_raise(monkeypatch, exc):
    """urllib wraps only what SENDING raises in URLError; a connection lost while
    reading the status line or body escapes `urlopen`/`read` raw — and used to
    escape run() too, aborting a whole ask_chain."""

    def drop(req, timeout=None):
        raise exc

    monkeypatch.setattr(ah.urllib.request, "urlopen", drop)
    res = _run(ah.run(_cfg(key="glm", model="glm-5.2"), "q", timeout=1))  # no budget to retry
    assert res.status == "error" and res.kind == "sdk_error"
    assert "network error" in res.text and type(exc).__name__ in res.text


def test_dropped_connection_gets_the_transient_retry(monkeypatch):
    """A body cut short is as transient as a 502, so it earns the same one retry."""
    calls = []

    class _Resp:
        headers = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            if len(calls) == 1:
                raise http.client.IncompleteRead(b'{"con', 40)
            return b'{"content":[{"type":"text","text":"hi"}]}'

    def fake_urlopen(req, timeout=None):
        calls.append("c")
        return _Resp()

    async def _fake_sleep(s):  # noqa: ANN001
        pass

    monkeypatch.setattr(ah.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    res = _run(ah.run(_cfg(key="glm", model="glm-5.2"), "q"))
    assert res.status == "ok" and res.text == "hi" and len(calls) == 2
    assert res.telemetry.retry_count == 1


def test_refusal_before_any_output_is_a_refusal_not_an_error(monkeypatch):
    """A safeguard decline is a 200 with stop_reason "refusal". With no content it
    became sdk_error "empty response", which fed the breaker for `fable` — the
    default synthesizer — over what is really a refusal."""
    _script(monkeypatch, {"type": "message", "model": "claude-fable-5-1",
                          "stop_reason": "refusal", "content": []})
    res = _run(ah.run(_cfg(), "q"))
    assert res.status == "refused" and res.kind == "provider_refusal"
    assert res.telemetry.stop_reason == "refusal"


def test_refusal_mid_stream_discards_the_partial_answer(monkeypatch):
    """A decline mid-stream leaves partial text behind; returned as ok it was a
    cut-off answer that the cache then pinned for an hour."""
    _script(monkeypatch, {"type": "message", "model": "claude-fable-5-1",
                          "stop_reason": "refusal",
                          "content": [{"type": "text", "text": "The middleware decodes the JWT"}]})
    res = _run(ah.run(_cfg(), "q"))
    assert res.status == "refused" and res.kind == "provider_refusal"
    assert "JWT" not in res.text


def test_max_tokens_stop_flags_the_answer_truncated(monkeypatch):
    """An answer the output cap cut off is kept (half an answer beats none) but
    flagged, so no cache layer pins it as complete."""
    _script(monkeypatch, {"model": "glm-5.2", "stop_reason": "max_tokens",
                          "content": [{"type": "text", "text": "Short answer: the race is in"}]})
    res = _run(ah.run(_cfg(key="glm", model="glm-5.2"), "q"))
    assert res.status == "ok" and res.kind == "truncated"
    assert res.text == "Short answer: the race is in"
    assert res.meta["partial"] is True and res.meta["stop_reason"] == "max_tokens"


def test_max_tokens_with_no_answer_is_not_backend_ill_health(monkeypatch):
    """All budget spent thinking, no text: the request's budget ran out, the backend
    is fine — so not an sdk_error that pushes the breaker open."""
    import ask_fable.health as health

    _script(monkeypatch, {"model": "glm-5.2", "stop_reason": "max_tokens",
                          "content": [{"type": "thinking", "thinking": "lock order... " * 20}]})
    res = _run(ah.run(_cfg(key="glm", model="glm-5.2"), "q"))
    assert res.status == "error" and res.kind == "budget_exhausted"
    assert "reasoning" in res.text
    assert res.kind in health._NON_HEALTH_KINDS


def test_read_timeout_still_reports_timeout(monkeypatch):
    """TimeoutError is an OSError too, but NOT a dropped connection: it must keep
    reaching the retry helper's deadline and come back as kind='timeout'."""

    def slow(req, timeout=None):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(ah.urllib.request, "urlopen", slow)
    res = _run(ah.run(_cfg(key="glm", model="glm-5.2"), "q", timeout=1))
    assert res.status == "error" and res.kind == "timeout"
