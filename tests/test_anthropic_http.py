"""ask_fable Anthropic-compatible HTTP oracle — config + response parsing."""

from __future__ import annotations

import asyncio
import json

import ask_fable.anthropic_http as ah
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
