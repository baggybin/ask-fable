"""The second wave of consolidation: stateful ask, councils, configure, context.

Each consolidated tool routes to the SAME existing per-backend handler, so the
internal labels (census cache key / hub session / audit title) are unchanged and
the old names stay callable as unadvertised aliases. No model is called.
"""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.server as server
from ask_fable.sessions import SessionStore


def _run(coro):
    return asyncio.run(coro)


# --- A: ask(oracle=…) absorbs ask_opus5 -----------------------------------


def test_ask_oracle_dispatches_to_opus_or_fable(monkeypatch):
    calls: dict = {}

    async def fake_opus(store, args):
        calls["opus"] = args
        return {"status": "ok"}

    async def fake_fable(store, args, **kw):
        calls["fable"] = kw
        return {"status": "ok"}

    monkeypatch.setattr(server, "_handle_opus5", fake_opus)
    monkeypatch.setattr(server, "_handle_claude_ask", fake_fable)
    store = SessionStore()

    assert _run(server._handle_ask(store, {"question": "q", "oracle": "opus"}))["status"] == "ok"
    assert "opus" in calls and "fable" not in calls

    assert _run(server._handle_ask(store, {"question": "q"}))["status"] == "ok"
    assert calls["fable"]["bridge"] is server.fable
    assert calls["fable"]["oracle_key"] == "fable"

    # opus5/opus55/opus48 all name the one Opus path.
    for spelling in ("opus5", "opus55", "opus48", "opus-5", "claude-opus-5"):
        calls.clear()
        _run(server._handle_ask(store, {"question": "q", "oracle": spelling}))
        assert "opus" in calls, spelling


def test_ask_rejects_a_non_multiturn_oracle(monkeypatch):
    store = SessionStore()
    out = _run(server._handle_ask(store, {"question": "q", "oracle": "minimax"}))
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert "ask_model" in out["detail"]


# --- B: ask_council(provider=…) absorbs the 4 provider councils -----------


def test_council_provider_scopes_and_preserves_the_title(monkeypatch):
    seen: dict = {}

    async def fake_council(question, context, selected, unknown, title, *a, **k):
        seen["title"] = title
        seen["selected"] = selected
        return {"status": "ok"}

    monkeypatch.setattr(server, "_council", fake_council)
    out = _run(
        server._handle_council(
            {"provider": "ollama", "question": "q", "models": ["gpt-oss:120b-cloud"]}
        )
    )
    assert out["status"] == "ok"
    assert seen["title"] == "ask_ollama_council"  # cache key + hub default preserved
    assert seen["selected"] == ["ollama:gpt-oss:120b-cloud"]


def test_council_provider_unknown_is_bad_args(monkeypatch):
    out = _run(server._handle_council({"provider": "nope", "question": "q"}))
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert "unknown council provider" in out["detail"]


@pytest.mark.parametrize(
    ("provider", "handler"),
    [
        ("ollama", "_handle_ollama_council"),
        ("atlas", "_handle_atlas_council"),
        ("openrouter", "_handle_openrouter_council"),
        ("lmstudio", "_handle_lms_council"),
    ],
)
def test_council_provider_map_covers_every_provider(monkeypatch, provider, handler):
    assert server._COUNCIL_PROVIDER_HANDLERS[provider] is getattr(server, handler)


# --- C: configure_council(provider=…) absorbs the 3 config writers --------


def test_configure_council_dispatches_per_provider(monkeypatch):
    seen: dict = {}

    def fake(name):
        def _w(args):
            seen["who"] = name
            seen["args"] = args
            return {"status": "ok"}

        return _w

    for prov in ("ollama", "atlas", "openrouter"):
        monkeypatch.setitem(server._CONFIGURE_COUNCIL_HANDLERS, prov, fake(prov))
    for prov in ("ollama", "atlas", "openrouter"):
        out = server._handle_configure_council({"provider": prov, "models": ["x"]})
        assert out["status"] == "ok"
        assert seen["who"] == prov
        assert "provider" not in seen["args"]  # not leaked to the writer


def test_configure_council_guards_provider_specific_args():
    out = server._handle_configure_council(
        {"provider": "ollama", "synthesizer": "codex"}
    )
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert "synthesizer" in out["detail"]

    out = server._handle_configure_council({"provider": "atlas", "default_model": "m"})
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert "default_model" in out["detail"]

    out = server._handle_configure_council({"provider": "bogus"})
    assert out["status"] == "error" and out["kind"] == "bad_args"


# --- D: context_read + context(op=…) --------------------------------------


def test_context_read_lists_when_no_key(monkeypatch):
    listed: dict = {}

    def fake_list(args):
        listed["hit"] = True
        return {"status": "ok", "count": 0, "entries": []}

    monkeypatch.setattr(server, "_handle_context_list", fake_list)
    out = server._handle_context_read({})
    assert listed["hit"] is True and out["status"] == "ok"


def test_context_read_with_key_reads(monkeypatch):
    monkeypatch.setattr(
        server.context_store, "get_meta", lambda k: ("value", 0.0, "desc")
    )
    out = server._handle_context_read({"key": "k"})
    assert out["status"] == "ok" and out["key"] == "k" and out["value"] == "value"


@pytest.mark.parametrize(
    ("op", "handler"),
    [("write", "_handle_context_write"), ("pack", "_handle_context_pack"),
     ("delete", "_handle_context_delete")],
)
def test_context_dispatch_per_op(monkeypatch, op, handler):
    seen: dict = {}

    def fake(args):
        seen["hit"] = True
        return {"status": "ok"}

    monkeypatch.setattr(server, handler, fake)
    out = server._handle_context({"op": op, "key": "k"})
    assert out["status"] == "ok" and seen["hit"] is True


def test_context_unknown_op_is_bad_args():
    out = server._handle_context({"op": "nope", "key": "k"})
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert "unknown context op" in out["detail"]


# --- alias remaps ----------------------------------------------------------


def test_legacy_aliases_remap_to_the_consolidated_tools():
    assert server._resolve_tool_alias("ask_opus5", {"question": "q"}) == (
        "ask",
        {"question": "q", "oracle": "opus"},
    )
    assert server._resolve_tool_alias("ask_atlas_council", {"question": "q"}) == (
        "ask_council",
        {"question": "q", "provider": "atlas"},
    )
    assert server._resolve_tool_alias("configure_openrouter_council", {"models": ["x"]}) == (
        "configure_council",
        {"models": ["x"], "provider": "openrouter"},
    )
    assert server._resolve_tool_alias("context_write", {"key": "k", "value": "v"}) == (
        "context",
        {"key": "k", "value": "v", "op": "write"},
    )
    assert server._resolve_tool_alias("context_list", {}) == ("context_read", {})
    # a tool with no alias passes through
    assert server._resolve_tool_alias("ask", {"question": "q"}) == ("ask", {"question": "q"})


# --- advertised surface ----------------------------------------------------


def _advertised_names():
    import mcp.types as types

    s = server.build_server()
    handler = s.request_handlers[types.ListToolsRequest]
    tools = _run(handler(types.ListToolsRequest(method="tools/list"))).root.tools
    return {t.name for t in tools}


def test_surface_is_the_consolidated_set():
    names = _advertised_names()
    assert len(names) == 28  # +ask_verify
    for new in (
        "ask",
        "ask_model",
        "ask_council",
        "ask_verify",
        "configure_council",
        "context",
        "context_read",
    ):
        assert new in names, new
    for gone in (
        "ask_opus5",
        "ask_ollama_council",
        "ask_atlas_council",
        "ask_openrouter_council",
        "ask_lms_council",
        "configure_ollama_council",
        "configure_atlas_council",
        "configure_openrouter_council",
        "context_write",
        "context_pack",
        "context_list",
        "context_delete",
    ):
        assert gone not in names, gone


def test_context_annotations_are_honest():
    by_name = {t.name: t.annotations for t in _advertised_tools()}
    # the read half stays read-only (auto-approvable)…
    assert by_name["context_read"].readOnlyHint is True
    assert by_name["context_read"].destructiveHint is False
    # …while the mutating half is flagged destructive.
    assert by_name["context"].readOnlyHint is False
    assert by_name["context"].destructiveHint is True


def _advertised_tools():
    import mcp.types as types

    s = server.build_server()
    handler = s.request_handlers[types.ListToolsRequest]
    return _run(handler(types.ListToolsRequest(method="tools/list"))).root.tools
