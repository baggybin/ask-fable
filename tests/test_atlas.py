"""ask_fable Atlas Cloud oracle — config gating + catalog parse + effort + run."""

from __future__ import annotations

import asyncio
import json

import ask_fable.atlas as at


def _run(coro):
    return asyncio.run(coro)


def test_api_key_precedence_and_defaults(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_ATLAS_API_KEY", raising=False)
    monkeypatch.delenv("ATLASCLOUD_API_KEY", raising=False)
    monkeypatch.delenv("ASK_FABLE_ATLAS_BASE_URL", raising=False)
    assert at.api_key() is None
    assert at.configured() is False  # chat endpoint needs a key
    assert at.base_url() == "https://api.atlascloud.ai"

    # falls back to the Atlas Cloud MCP server's var
    monkeypatch.setenv("ATLASCLOUD_API_KEY", "from-mcp")
    assert at.api_key() == "from-mcp"
    assert at.configured() is True

    # ask_fable's own var wins over the fallback
    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "from-ask-fable")
    assert at.api_key() == "from-ask-fable"

    # base url trims trailing slash
    monkeypatch.setenv("ASK_FABLE_ATLAS_BASE_URL", "https://api.atlascloud.ai/")
    assert at.base_url() == "https://api.atlascloud.ai"


def test_default_model_precedence(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_ATLAS_MODEL", raising=False)
    assert at.default_model() == "xai/grok-4.6"
    monkeypatch.setenv("ASK_FABLE_ATLAS_MODEL", "openai/gpt-5.6-sol")
    assert at.default_model() == "openai/gpt-5.6-sol"


def test_council_models_env_parsing_and_config_precedence(monkeypatch):
    from ask_fable import config

    monkeypatch.delenv("ASK_FABLE_ATLAS_COUNCIL", raising=False)
    assert at.council_models() == []  # nothing configured — caller derives a panel
    # comma/space separated, atlas: prefix stripped; casing preserved (Atlas ids
    # are case-sensitive) with a case-insensitive dedupe keeping first-seen
    monkeypatch.setenv(
        "ASK_FABLE_ATLAS_COUNCIL",
        "atlas:deepseek-ai/DeepSeek-V3.1-Terminus, moonshotai/kimi-k2.6 DEEPSEEK-ai/deepseek-v3.1-terminus",
    )
    assert at.council_models() == ["deepseek-ai/DeepSeek-V3.1-Terminus", "moonshotai/kimi-k2.6"]
    # config file wins over the env var
    config.save({"atlas_council": ["deepseek-ai/deepseek-v4-pro"]})
    assert at.council_models() == ["deepseek-ai/deepseek-v4-pro"]


def test_synthesizer_token_precedence(monkeypatch):
    from ask_fable import config

    monkeypatch.delenv("ASK_FABLE_ATLAS_SYNTHESIZER", raising=False)
    assert at.synthesizer_token() is None  # keeps the built-in GPT-first ladder
    monkeypatch.setenv("ASK_FABLE_ATLAS_SYNTHESIZER", "codex")
    assert at.synthesizer_token() == "codex"
    config.save({"atlas_synthesizer": "fable"})
    assert at.synthesizer_token() == "fable"


def test_effort_presets():
    assert at.effort_preset("quick")["max_tokens"] == 1024
    assert at.effort_preset("standard")["max_tokens"] == 4096
    deep = at.effort_preset("deep")
    assert deep["max_tokens"] == 16384 and deep.get("reasoning_effort") == "high"
    # unknown / empty falls back to deep (max effort by default)
    assert at.effort_preset("bogus") is at.EFFORT_PRESETS["deep"]
    assert at.effort_preset(None) is at.EFFORT_PRESETS["deep"]
    assert at.default_effort() == "deep"
    assert at.effort_preset("  STANDARD ") is at.EFFORT_PRESETS["standard"]
    # the menu exposes all three; deep is labeled default
    assert [e["value"] for e in at.effort_menu()] == ["quick", "standard", "deep"]
    assert "default" in at.effort_menu()[2]["label"].lower()


def test_parse_message_and_usage():
    body = json.dumps(
        {
            "model": "xai/grok-4.5",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "the answer"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }
    ).encode()
    text, parsed, err = at._parse(body)
    assert text == "the answer" and err is None
    assert at._finish_reason(parsed) == "stop"


def test_parse_error_envelope():
    text, _parsed, err = at._parse(
        b'{"error":{"type":"invalid_request_error","message":"bad key"}}'
    )
    assert text is None and "bad key" in err


def test_parse_garbage():
    text, _parsed, err = at._parse(b"not json")
    assert text is None and "unparseable" in err


def test_parse_no_choices():
    text, _parsed, err = at._parse(b'{"choices": []}')
    assert text is None and "choices" in err


def test_run_not_configured(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_ATLAS_API_KEY", raising=False)
    monkeypatch.delenv("ATLASCLOUD_API_KEY", raising=False)
    res = _run(at.run("xai/grok-4.5", "q"))
    assert res.status == "error" and res.kind == "not_configured"
    assert "ATLAS" in res.text  # names either env var


def test_run_ok(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {
                    "model": "xai/grok-4.5",
                    "choices": [{"message": {"role": "assistant", "content": "the answer"}}],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
                }
            ).encode()

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    monkeypatch.setattr(at.urllib.request, "urlopen", fake_urlopen)
    res = _run(at.run("xai/grok-4.5", "How are handlers registered?"))
    assert res.status == "ok" and res.text == "the answer" and res.model == "xai/grok-4.5"
    assert seen["url"] == "https://api.atlascloud.ai/v1/chat/completions"
    assert seen["headers"].get("authorization") == "Bearer k"
    assert seen["body"]["model"] == "xai/grok-4.5" and seen["body"]["stream"] is False
    assert seen["body"]["messages"][0]["role"] == "system"
    assert seen["body"]["messages"][1]["role"] == "user"
    # default effort is deep = max_tokens + reasoning_effort high
    assert seen["body"].get("reasoning_effort") == "high"
    assert seen["body"]["max_tokens"] == 16384
    # usage telemetry recorded
    assert res.telemetry is not None and res.telemetry.transport == "http-json"
    assert res.telemetry.usage is not None and res.telemetry.usage.total_tokens == 10


def test_run_deep_sends_and_retries_reasoning_effort(monkeypatch):
    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok now"}}]}).encode()

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append("reasoning_effort" in body)
        if "reasoning_effort" in body:
            import urllib.error

            raise urllib.error.HTTPError(req.full_url, 400, "unsupported field", {}, None)
        return _Resp()

    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    monkeypatch.setattr(at.urllib.request, "urlopen", fake_urlopen)
    res = _run(at.run("xai/grok-4.5", "q", effort="deep"))
    assert res.status == "ok" and res.text == "ok now"
    assert calls == [True, False]  # first with reasoning_effort, retried without
    assert res.telemetry is not None and res.telemetry.retry_count == 1


def test_run_standard_does_not_probe_reasoning_effort(monkeypatch):
    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ans"}}]}).encode()

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append("reasoning_effort" in body)
        return _Resp()

    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    monkeypatch.setattr(at.urllib.request, "urlopen", fake_urlopen)
    res = _run(at.run("xai/grok-4.5", "q", effort="standard"))
    assert res.status == "ok"
    assert calls == [False]  # never probes on standard
    assert res.telemetry.retry_count == 0


def test_run_http_error(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "bad")
    monkeypatch.setattr(at.urllib.request, "urlopen", boom)
    res = _run(at.run("xai/grok-4.5", "q"))
    assert res.status == "error" and res.kind == "auth_failed"
    assert (
        "Atlas request failed" in res.text and "401" in res.text
    )  # detail surfaced, not swallowed
    assert res.telemetry is not None and res.telemetry.http_status == 401


def test_run_http_429_is_rate_limit(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    monkeypatch.setattr(at.urllib.request, "urlopen", boom)

    async def _fake_sleep(s):  # noqa: ANN001
        pass

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    res = _run(at.run("xai/grok-4.5", "q"))
    assert res.status == "error" and res.kind == "rate_limit"
    assert "429" in res.text
    assert res.telemetry is not None and res.telemetry.http_status == 429
    assert res.telemetry.retry_count == 1


def test_run_retries_once_on_502(monkeypatch):
    import urllib.error

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"hi"}}]}'

    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append("c")
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)
        return _Resp()

    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    monkeypatch.setattr(at.urllib.request, "urlopen", fake_urlopen)

    async def _fake_sleep2(s):  # noqa: ANN001
        pass

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep2)
    res = _run(at.run("xai/grok-4.5", "q"))
    assert res.status == "ok" and res.text == "hi"
    assert len(calls) == 2
    assert (
        res.telemetry is not None
        and res.telemetry.retry_count == 1
        and res.telemetry.http_status == 200
    )


def test_model_item_and_featured():
    items_raw = [
        {
            "model": "a/1",
            "displayName": "A1",
            "organization": "XAI",
            "tags": ["HOT"],
            "price": {"actual": {"input_price": "2", "output_price": "6", "cache_price": "0.5"}},
            "contextLength": 500000,
            "avgLatency": 0.5,
            "type": "Text",
            "display_console": True,
        },
        {
            "model": "b/2",
            "displayName": "B2",
            "organization": "OPENAI",
            "tags": ["NEW"],
            "price": {"actual": {"input_price": "5", "output_price": "30"}},
            "contextLength": 1000000,
            "type": "Text",
            "display_console": True,
        },
        {
            "model": "c/3",
            "displayName": "C3",
            "organization": "XAI",
            "tags": ["LLM"],
            "price": {"actual": {"input_price": "1", "output_price": "2"}},
            "type": "Text",
            "display_console": True,
        },
    ]
    parsed = [at._model_item(m) for m in items_raw]
    a1 = parsed[0]
    assert a1["model_id"] == "a/1" and a1["cost_note"] == "$2/$6 per M"
    assert a1["provider"] == "XAI" and "HOT" in a1["tags"]
    assert "500k ctx" in a1["description"]
    # featured: HOT/NEW one-per-provider first (a1=XAI, b2=OPENAI), then fill
    feat = at._featured(parsed, limit=2)
    feat_ids = [it["model_id"] for it in feat]
    assert feat_ids == ["a/1", "b/2"]


def test_catalog_filters_and_shapes(monkeypatch):
    payload = {
        "code": 200,
        "data": [
            {
                "model": "xai/grok-4.5",
                "displayName": "Grok 4.5",
                "organization": "XAI",
                "tags": ["HOT", "NEW"],
                "price": {"actual": {"input_price": "2", "output_price": "6"}},
                "contextLength": 500000,
                "type": "Text",
                "display_console": True,
            },
            {
                "model": "hidden/1",
                "displayName": "Hidden",
                "organization": "X",
                "type": "Text",
                "display_console": False,
            },  # filtered
            {
                "model": "img/1",
                "displayName": "Image",
                "type": "Image",
                "display_console": True,
            },  # not text
        ],
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    def fake_urlopen(req, timeout=None):
        return _Resp()

    monkeypatch.setattr(at.urllib.request, "urlopen", fake_urlopen)
    cat = at.catalog()
    assert cat["cloud_ok"] is True
    assert cat["models"] == ["xai/grok-4.5"]  # hidden + image filtered
    assert cat["menu"][0]["model_id"] == "xai/grok-4.5"
    assert cat["menu"][0]["cost_note"] == "$2/$6 per M"
    assert [e["value"] for e in cat["effort_choices"]] == ["quick", "standard", "deep"]
    assert cat["featured"][0]["model_id"] == "xai/grok-4.5"
    assert cat["balance_note"] is None


def test_recommend_models_ranks_task_fit_and_returns_picker_options():
    # Given: live-catalog-shaped entries with different advertised strengths.
    items = [
        at._model_item(
            {
                "model": "vendor/general",
                "displayName": "General",
                "organization": "GENERAL",
                "tags": ["HOT"],
                "profile": "Conversational model for everyday chat.",
                "contextLength": 100_000,
                "type": "Text",
            }
        ),
        at._model_item(
            {
                "model": "vendor/coder",
                "displayName": "Coder",
                "organization": "CODER",
                "tags": ["CODE", "NEW"],
                "profile": "Coding agent for repository-scale software engineering and debugging.",
                "contextLength": 250_000,
                "type": "Text",
            }
        ),
    ]

    # When: the user asks for a model suited to a repository debugging task.
    recommendations = at.recommend_models(
        [item for item in items if item is not None],
        "debug a large Rust repository",
        limit=2,
    )

    # Then: the coding model leads and every result is ready for a picker.
    assert [item["model_id"] for item in recommendations] == [
        "vendor/coder",
        "vendor/general",
    ]
    assert recommendations[0]["match_reasons"]
    assert recommendations[0]["picker_description"]


def test_catalog_adds_task_recommendations(monkeypatch):
    # Given: a reachable catalog with one coding model and one chat model.
    payload = {
        "code": 200,
        "data": [
            {
                "model": "vendor/chat",
                "displayName": "Chat",
                "organization": "CHAT",
                "tags": ["HOT"],
                "profile": "Natural conversation and everyday assistance.",
                "type": "Text",
                "display_console": True,
            },
            {
                "model": "vendor/code",
                "displayName": "Code",
                "organization": "CODE",
                "tags": ["CODE", "NEW"],
                "profile": "Software engineering, repository understanding, and debugging.",
                "type": "Text",
                "display_console": True,
            },
        ],
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr(at.urllib.request, "urlopen", lambda req, timeout=None: _Resp())

    # When: catalog discovery includes a task.
    cat = at.catalog(task="refactor a Python service", recommendation_limit=2)

    # Then: task-ranked recommendations and a machine-readable picker are returned.
    assert cat["recommendations"][0]["model_id"] == "vendor/code"
    assert cat["picker"]["model_options"][0]["value"] == "vendor/code"
    assert cat["picker"]["effort_options"] == at.effort_menu()
    assert cat["recommendation_basis"] == "live_catalog_metadata"


def test_recommend_models_prefers_provider_diversity():
    # Given: two strong coding models from one provider and one from another.
    items = [
        at._model_item(
            {
                "model": "same/coder-pro",
                "displayName": "Coder Pro",
                "organization": "SAME",
                "tags": ["CODE", "HOT"],
                "profile": "Repository-scale coding and debugging.",
                "type": "Text",
            }
        ),
        at._model_item(
            {
                "model": "same/coder-air",
                "displayName": "Coder Air",
                "organization": "SAME",
                "tags": ["CODE", "NEW"],
                "profile": "Fast coding and debugging.",
                "type": "Text",
            }
        ),
        at._model_item(
            {
                "model": "other/coder",
                "displayName": "Other Coder",
                "organization": "OTHER",
                "tags": ["CODE"],
                "profile": "Software engineering and debugging.",
                "type": "Text",
            }
        ),
    ]

    # When: two task recommendations are requested.
    recommendations = at.recommend_models(
        [item for item in items if item is not None],
        "debug a repository",
        limit=2,
    )

    # Then: the shortlist covers two providers instead of near-duplicate siblings.
    assert {item["provider"] for item in recommendations} == {"SAME", "OTHER"}


def test_catalog_unreachable(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(at.urllib.request, "urlopen", boom)
    cat = at.catalog()
    assert cat["cloud_ok"] is False and cat["menu"] == []
    assert "hint" in cat
    # effort choices still present so an agent can fall back
    assert cat["effort_choices"] and cat["models"] == []


def test_run_deep_permanent_400_not_retried(monkeypatch):
    """A 400 that doesn't read like a rejected reasoning_effort field (e.g.
    context length exceeded) must not be re-POSTed without the field — the
    identical retry fails identically and just burns quota twice."""
    import io
    import urllib.error

    calls = []

    def boom(req, timeout=None):
        calls.append("c")
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {}, io.BytesIO(b"context length exceeded")
        )

    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    monkeypatch.setattr(at.urllib.request, "urlopen", boom)
    res = _run(at.run("xai/grok-4.5", "q", effort="deep"))
    assert res.status == "error"
    assert calls == ["c"]  # exactly one upstream POST
    assert res.telemetry is not None and res.telemetry.retry_count == 0
