"""ask_fable Ollama Cloud oracle — config gating + response parsing + run."""

from __future__ import annotations

import asyncio
import json

import ask_fable.ollama as ol


def _run(coro):
    return asyncio.run(coro)


def test_api_key_and_defaults(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("ASK_FABLE_OLLAMA_BASE_URL", raising=False)
    assert ol.api_key() is None
    # default endpoint is the local daemon — no key required, reachable via signin
    assert ol.base_url() == "http://localhost:11434"
    assert ol.is_local() is True and ol.configured() is True
    monkeypatch.setenv("ASK_FABLE_OLLAMA_API_KEY", "secret")
    assert ol.api_key() == "secret"
    monkeypatch.setenv("ASK_FABLE_OLLAMA_BASE_URL", "https://ollama.com/")
    assert ol.base_url() == "https://ollama.com"  # trailing slash trimmed
    assert ol.is_local() is False and ol.configured() is True  # remote + key
    monkeypatch.setenv("ASK_FABLE_OLLAMA_MODEL", "qwen3-coder:cloud")
    assert ol.default_model() == "qwen3-coder:cloud"


def test_remote_without_key_unconfigured(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("ASK_FABLE_OLLAMA_BASE_URL", "https://ollama.com")
    assert ol.is_local() is False and ol.configured() is False


def test_parse_message_and_thinking():
    body = json.dumps(
        {
            "model": "kimi-k2.7-code:cloud",
            "message": {"role": "assistant", "content": "the answer", "thinking": "hmm"},
            "done": True,
        }
    ).encode()
    text, thinking, err = ol._parse(body)
    assert text == "the answer" and thinking == "hmm" and err is None


def test_parse_error_shape():
    text, thinking, err = ol._parse(b'{"error": "model not found"}')
    assert text is None and err == "model not found"


def test_parse_garbage():
    text, thinking, err = ol._parse(b"not json")
    assert text is None and "unparseable" in err


def test_run_not_configured(monkeypatch):
    # remote endpoint + no key -> not_configured
    monkeypatch.delenv("ASK_FABLE_OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("ASK_FABLE_OLLAMA_BASE_URL", "https://ollama.com")
    res = _run(ol.run("gpt-oss:120b-cloud", "q"))
    assert res.status == "error" and res.kind == "not_configured"
    assert "ASK_FABLE_OLLAMA_API_KEY" in res.text


def test_run_ok(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"message":{"role":"assistant","content":"the answer"}}'

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setenv("ASK_FABLE_OLLAMA_API_KEY", "k")
    monkeypatch.setenv("ASK_FABLE_OLLAMA_BASE_URL", "https://ollama.com")
    monkeypatch.setattr(ol.urllib.request, "urlopen", fake_urlopen)
    res = _run(ol.run("kimi-k2.7-code:cloud", "How are handlers registered?"))
    assert res.status == "ok" and res.text == "the answer" and res.model == "kimi-k2.7-code:cloud"
    assert seen["url"] == "https://ollama.com/api/chat"
    assert seen["headers"].get("authorization") == "Bearer k"
    assert seen["body"]["model"] == "kimi-k2.7-code:cloud" and seen["body"]["stream"] is False
    assert seen["body"]["messages"][0]["role"] == "system"
    assert seen["body"]["options"]["num_predict"] == 65536  # output cap


def test_run_local_no_key_sends_no_auth(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"message":{"content":"local answer"}}'

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    # default endpoint (local daemon), no key: reachable, no Authorization header
    monkeypatch.delenv("ASK_FABLE_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("ASK_FABLE_OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr(ol.urllib.request, "urlopen", fake_urlopen)
    res = _run(ol.run("gpt-oss:120b-cloud", "q about routing"))
    assert res.status == "ok" and res.text == "local answer"
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert "authorization" not in seen["headers"]


def test_run_http_error(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setenv("ASK_FABLE_OLLAMA_API_KEY", "bad")
    monkeypatch.setattr(ol.urllib.request, "urlopen", boom)
    res = _run(ol.run("gpt-oss:120b-cloud", "q"))
    assert res.status == "error" and res.kind == "auth_failed"
    assert "Ollama request failed" in res.text and "401" in res.text  # detail surfaced
    assert res.telemetry is not None and res.telemetry.http_status == 401


def test_run_retries_without_think_on_400(monkeypatch):
    import urllib.error

    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"message":{"content":"ok now"}}'

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append("think" in body)
        if "think" in body:
            raise urllib.error.HTTPError(req.full_url, 400, "unsupported think", {}, None)
        return _Resp()

    monkeypatch.setenv("ASK_FABLE_OLLAMA_API_KEY", "k")
    monkeypatch.setattr(ol.urllib.request, "urlopen", fake_urlopen)
    res = _run(ol.run("mistral-large-3:675b-cloud", "q"))
    assert res.status == "ok" and res.text == "ok now"
    assert calls == [True, False]  # first with think, retried without
    assert res.telemetry is not None and res.telemetry.retry_count == 1


def test_run_retries_once_on_429(monkeypatch):
    import urllib.error

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"message":{"content":"ok now"}}'

    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append("c")
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        return _Resp()

    monkeypatch.setenv("ASK_FABLE_OLLAMA_API_KEY", "k")
    monkeypatch.setattr(ol.urllib.request, "urlopen", fake_urlopen)

    async def _fake_sleep(s):  # noqa: ANN001
        pass

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    res = _run(ol.run("mistral-large-3:675b-cloud", "q"))
    assert res.status == "ok" and res.text == "ok now"
    assert len(calls) == 2
    assert (
        res.telemetry is not None
        and res.telemetry.retry_count == 1
        and res.telemetry.http_status == 200
    )
