"""The consolidated tools: `ask_model` (one stateless oracle) and `list_models`.

These replace the per-backend tools (ask_m3, ask_glm, ask_atlas, list_atlas_models,
…). The dedicated tools stay callable as unadvertised aliases; the point of these
tests is that the consolidation is SURFACE-ONLY — the internal labels (audit
`tool`, hub `session`, cache key) are unchanged, so stats and cache continuity
survive. No model is called.
"""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    # Never serve or pin a tool-cache entry: the dispatch keys are asserted
    # elsewhere, and a real SQLite hit would skip the stubbed oracle call.
    monkeypatch.setattr(server.cache, "get", lambda k: None)
    monkeypatch.setattr(server.cache, "put", lambda k, p: None)


def _run(coro):
    return asyncio.run(coro)


def _capture_run(monkeypatch):
    seen: dict = {}

    async def fake_run(key, question, context="", *, effort=None, model=None):
        seen["key"] = key
        seen["effort"] = effort
        seen["model"] = model
        return OracleResult("ok", key=key, text="answer", model=key)

    monkeypatch.setattr(server.oracles, "run", fake_run)
    return seen


def _capture_lookup(monkeypatch):
    seen: dict = {}

    def fake_lookup(tool, models, question, context, effort=None):
        seen["tool"] = tool
        seen["models"] = models
        return ("cache-key", None)

    monkeypatch.setattr(server, "_cache_lookup", fake_lookup)
    return seen


# --- ask_model: provider dispatch -----------------------------------------


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("minimax", None, "minimax"),
        ("glm", None, "glm"),
        ("deepseek", None, "deepseek"),
        ("gemini", None, "gemini"),
        ("codex", None, "codex"),
        ("sonnet", None, "sonnet"),
        ("grok", "grok-4.6", "grok"),
        ("kimi", "kimi-code/k3", "kimi"),
        ("ollama", "qwen3-coder:480b-cloud", "ollama:qwen3-coder:480b-cloud"),
        ("lmstudio", "qwen/qwen3.8-27b", "lmstudio:qwen/qwen3.8-27b"),
        ("atlas", "zai-org/glm-5.2", "atlas:zai-org/glm-5.2"),
        ("ali", "qwen3.8-max", "ali:qwen3.8-max"),
        ("openrouter", "anthropic/claude-fable-5.1", "openrouter:anthropic/claude-fable-5.1"),
    ],
)
def test_provider_dispatches_to_the_right_oracle_key(monkeypatch, provider, model, expected):
    seen = _capture_run(monkeypatch)
    args = {"provider": provider, "question": "How does routing work?"}
    if model is not None:
        args["model"] = model
    out = _run(server._handle_model(args))
    assert out["status"] == "ok"
    assert seen["key"] == expected


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("m3", "minimax"), ("gpt", "codex"), ("xai", "grok"), ("claude-sonnet-5", "sonnet")],
)
def test_provider_aliases_resolve(monkeypatch, provider, expected):
    seen = _capture_run(monkeypatch)
    out = _run(server._handle_model({"provider": provider, "question": "q"}))
    assert out["status"] == "ok"
    assert seen["key"] == expected


def test_grok_and_kimi_forward_model_and_effort(monkeypatch):
    seen = _capture_run(monkeypatch)
    _run(
        server._handle_model(
            {"provider": "grok", "model": "grok-4.6", "effort": "quick", "question": "q"}
        )
    )
    assert seen["key"] == "grok" and seen["model"] == "grok-4.6" and seen["effort"] == "quick"


def test_fixed_model_provider_rejects_a_model_argument(monkeypatch):
    _capture_run(monkeypatch)
    out = _run(server._handle_model({"provider": "minimax", "model": "MiniMax-M3", "question": "q"}))
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert "fixed model" in out["detail"]


def test_multiturn_provider_points_at_ask(monkeypatch):
    _capture_run(monkeypatch)
    for provider in ("fable", "opus", "opus5", "opus48"):
        out = _run(server._handle_model({"provider": provider, "question": "q"}))
        assert out["status"] == "error" and out["kind"] == "bad_args"
        assert "`ask`" in out["detail"] or "ask_opus5" in out["detail"]


def test_unknown_provider_is_bad_args(monkeypatch):
    out = _run(server._handle_model({"provider": "nope", "question": "q"}))
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert "unknown provider" in out["detail"]


# --- ask_model: internal labels are preserved (cache/audit/stats) ----------


def test_internal_tool_and_model_labels_are_preserved(monkeypatch):
    """`ask_model(provider="minimax")` must produce the SAME cache key inputs as the
    old `ask_m3`: tool label "ask_m3" and model ["MiniMax-M3"]. If it passed
    tool="ask_model" the cache key would change and every cached answer would miss."""
    _capture_run(monkeypatch)
    lookup = _capture_lookup(monkeypatch)
    out = _run(server._handle_model({"provider": "minimax", "question": "q"}))
    assert out["status"] == "ok"
    assert lookup["tool"] == "ask_m3"
    assert lookup["models"] == [server.minimax.minimax_model()]


def test_cache_key_matches_the_legacy_tool(monkeypatch):
    from ask_fable import cache

    assert cache.key("ask_m3", ["MiniMax-M3"], "q", "c") == cache.key(
        "ask_m3", ["MiniMax-M3"], "q", "c"
    )
    # The consolidated tool reuses the legacy label, so the two are the same key.
    _capture_run(monkeypatch)
    lookup = _capture_lookup(monkeypatch)
    _run(server._handle_model({"provider": "minimax", "question": "q"}))
    legacy = cache.key("ask_m3", lookup["models"], "q", "")
    consolidated = cache.key(lookup["tool"], lookup["models"], "q", "")
    assert legacy == consolidated


# --- ask_model: schema + legacy aliases ------------------------------------


def test_schema_requires_provider_and_rejects_unknown():
    assert server._schema_error("ask_model", {"question": "q"}) == (
        "missing required argument: provider"
    )
    assert server._schema_error("ask_model", {"provider": "bogus", "question": "q"}) is not None
    assert server._schema_error("ask_model", {"provider": "minimax", "question": "q"}) is None
    assert server._schema_error("ask_model", {"provider": "minimax"}) == (
        "missing required argument: question"
    )


def test_legacy_tool_names_remap_to_the_consolidated_tool():
    name, args = server._resolve_tool_alias("ask_m3", {"question": "q"})
    assert name == "ask_model" and args["provider"] == "minimax"
    name, args = server._resolve_tool_alias("ask_lms", {"question": "q", "model": "m"})
    assert name == "ask_model" and args["provider"] == "lmstudio" and args["model"] == "m"
    name, args = server._resolve_tool_alias("list_atlas_models", {"task": "x"})
    assert name == "list_models" and args["provider"] == "atlas"
    # A non-legacy name passes through untouched.
    name, args = server._resolve_tool_alias("ask", {"question": "q"})
    assert name == "ask" and args == {"question": "q"}


def test_legacy_schemas_are_kept_for_validation():
    assert "ask_m3" in server._TOOL_SCHEMAS and "list_atlas_models" in server._TOOL_SCHEMAS
    assert server._schema_error("ask_lms", {}) is not None


# --- list_models -----------------------------------------------------------


def test_list_models_dispatches_per_provider(monkeypatch):
    async def fake_ali(args):
        return {"status": "ok", "who": "ali"}

    async def fake_atlas(args):
        return {"status": "ok", "who": "atlas"}

    async def fake_openrouter(args):
        return {"status": "ok", "who": "openrouter"}

    async def fake_ollama(args):
        return {"status": "ok", "who": "ollama"}

    async def fake_lms(args):
        return {"status": "ok", "who": "lmstudio"}

    monkeypatch.setitem(server._LIST_MODELS_PROVIDERS, "ali", fake_ali)
    monkeypatch.setitem(server._LIST_MODELS_PROVIDERS, "atlas", fake_atlas)
    monkeypatch.setitem(server._LIST_MODELS_PROVIDERS, "openrouter", fake_openrouter)
    monkeypatch.setitem(server._LIST_MODELS_PROVIDERS, "ollama", fake_ollama)
    monkeypatch.setitem(server._LIST_MODELS_PROVIDERS, "lmstudio", fake_lms)

    for provider, who in [
        ("ali", "ali"),
        ("atlas", "atlas"),
        ("openrouter", "openrouter"),
        ("ollama", "ollama"),
        ("lmstudio", "lmstudio"),
    ]:
        out = _run(server._handle_list_models({"provider": provider}))
        assert out["who"] == who


def test_list_models_unknown_provider_and_schema():
    out = _run(server._handle_list_models({"provider": "bogus"}))
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert server._schema_error("list_models", {}) == "missing required argument: provider"
    assert server._schema_error("list_models", {"provider": "bogus"}) is not None
    assert server._schema_error("list_models", {"provider": "atlas"}) is None


# --- advertised surface ----------------------------------------------------


def _advertised_names():
    import mcp.types as types

    s = server.build_server()
    handler = s.request_handlers[types.ListToolsRequest]
    tools = _run(handler(types.ListToolsRequest(method="tools/list"))).root.tools
    return {t.name for t in tools}


def test_consolidated_tools_are_advertised_and_legacy_names_are_not():
    names = _advertised_names()
    assert "ask_model" in names and "list_models" in names
    for gone in (
        "ask_m3",
        "ask_glm",
        "ask_deepseek",
        "ask_gemini",
        "ask_codex",
        "ask_grok",
        "ask_kimi",
        "ask_ollama",
        "ask_lms",
        "ask_atlas",
        "ask_ali",
        "ask_openrouter",
        "ask_sonnet",
        "list_ali_models",
        "list_atlas_models",
        "list_openrouter_models",
        "list_ollama_models",
        "list_lms_models",
    ):
        assert gone not in names, gone
    # ask_websearch stays a first-class, separately-advertised tool.
    assert "ask_websearch" in names
