"""ask_fable atlas council handlers — ask_atlas_council member/adjudicator
resolution and configure_atlas_council persistence. Guard + backends stubbed."""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _quiet_and_no_audit(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


@pytest.fixture(autouse=True)
def _no_atlas_keys(monkeypatch):
    """Deterministic regardless of the developer's real Atlas credentials."""
    monkeypatch.delenv("ASK_FABLE_ATLAS_API_KEY", raising=False)
    monkeypatch.delenv("ATLASCLOUD_API_KEY", raising=False)
    monkeypatch.delenv("ASK_FABLE_ATLAS_COUNCIL", raising=False)
    monkeypatch.delenv("ASK_FABLE_ATLAS_SYNTHESIZER", raising=False)


def _run(coro):
    return asyncio.run(coro)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def _stub_panel(monkeypatch):
    """Stub the fan-out (any oracle key answers) and a Fable synthesis turn."""
    keys = []

    async def fake_oracle_run(key, question, context=""):
        keys.append(key)
        return OracleResult("ok", key=key, text=f"{key} ans", model=server.oracles.label(key))

    async def fake_fable(question, context="", *, resume=None, system_prompt=None, on_think=None):
        return OracleResult("ok", text="MERGED atlas")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    return keys


def test_atlas_council_normalizes_and_synthesizes(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    keys = _stub_panel(monkeypatch)
    out = _run(server._handle_atlas_council(
        {"question": "How does routing work?",
         "models": ["zai-org/glm-5.2", "atlas:deepseek-ai/deepseek-v4-pro", "ZAI-ORG/glm-5.2"],
         "synthesizer": "fable"}
    ))
    assert out["status"] == "ok" and out["answer"] == "MERGED atlas"
    # bare ids get the atlas: prefix; dupes collapse case-insensitively,
    # keeping the first-seen spelling
    assert set(keys) == {"atlas:zai-org/glm-5.2", "atlas:deepseek-ai/deepseek-v4-pro"}
    assert set(out["sources"]) == {"atlas:zai-org/glm-5.2", "atlas:deepseek-ai/deepseek-v4-pro"}
    assert out["synthesis"]["note"] == "explicit synthesizer"


def test_atlas_council_preserves_id_casing(monkeypatch):
    # Atlas ids are case-SENSITIVE (deepseek-ai/DeepSeek-V3.1-Terminus): the id
    # must reach the fan-out verbatim, or Atlas answers HTTP 400 "not found".
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    keys = _stub_panel(monkeypatch)
    out = _run(server._handle_atlas_council(
        {"question": "How does routing work?",
         "models": ["deepseek-ai/DeepSeek-V3.1-Terminus", "Qwen/Qwen3-235B-A22B-Instruct-2507"],
         "synthesizer": "fable"}
    ))
    assert out["status"] == "ok"
    assert set(keys) == {
        "atlas:deepseek-ai/DeepSeek-V3.1-Terminus",
        "atlas:Qwen/Qwen3-235B-A22B-Instruct-2507",
    }


def test_atlas_council_defaults_to_configured_set(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    monkeypatch.setattr(
        server.atlas, "council_models",
        lambda: ["zai-org/glm-5.2", "moonshotai/kimi-k2"],
    )
    keys = _stub_panel(monkeypatch)
    out = _run(server._handle_atlas_council(
        {"question": "How does routing work here?", "synthesizer": "fable"}
    ))
    assert out["status"] == "ok"
    assert set(keys) == {"atlas:zai-org/glm-5.2", "atlas:moonshotai/kimi-k2"}


def test_atlas_council_derives_panel_from_featured_catalog(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    monkeypatch.setattr(server.atlas, "council_models", lambda: [])
    monkeypatch.setattr(
        server.atlas, "catalog",
        lambda: {"cloud_ok": True, "featured": [
            {"model_id": "prov1/model-a"}, {"model_id": "prov2/model-b"},
            {"model_id": "prov3/model-c"}, {"model_id": "prov4/model-d"},
        ]},
    )
    keys = _stub_panel(monkeypatch)
    out = _run(server._handle_atlas_council(
        {"question": "How does routing work here?", "synthesizer": "fable"}
    ))
    assert out["status"] == "ok"
    # top 3 featured (already one per provider), not the whole shortlist
    assert set(keys) == {"atlas:prov1/model-a", "atlas:prov2/model-b", "atlas:prov3/model-c"}


def test_atlas_council_catalog_dead_and_nothing_configured(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.atlas, "council_models", lambda: [])
    monkeypatch.setattr(server.atlas, "catalog", lambda: {"cloud_ok": False, "featured": []})
    out = _run(server._handle_atlas_council({"question": "trace the router please"}))
    assert out["status"] == "error" and out["kind"] == "no_models"
    assert "configure_atlas_council" in out["detail"]


def test_atlas_council_not_configured_guard(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.grok, "available", lambda: False)

    async def boom(key, question, context=""):
        raise AssertionError("no oracle may run when atlas is unconfigured")

    monkeypatch.setattr(server.oracles, "run", boom)
    out = _run(server._handle_atlas_council(
        {"question": "How does routing work?", "models": ["zai-org/glm-5.2"]}
    ))
    assert out["status"] == "error" and out["kind"] == "not_configured"
    assert "ASK_FABLE_ATLAS_API_KEY" in out["detail"]


def test_atlas_council_grok_only_panel_runs_keyless(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.grok, "available", lambda: True)
    monkeypatch.setattr(server.oracles, "available", lambda k: False)  # codex CLI absent
    keys = _stub_panel(monkeypatch)
    out = _run(server._handle_atlas_council(
        {"question": "How does routing work?", "models": ["xai/grok-4.5", "xai/grok-4"]}
    ))
    # every member reroutes to the local grok CLI, so no Atlas key is needed;
    # the adjudicator ladder bottoms out at fable (no codex, no atlas key)
    assert out["status"] == "ok"
    assert set(keys) == {"atlas:xai/grok-4.5", "atlas:xai/grok-4"}
    assert out["synthesis"]["used"] == "fable"
    assert "unconfigured" in out["synthesis"]["note"]


def test_atlas_council_prefers_local_codex_adjudicator(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    keys = _stub_panel(monkeypatch)
    monkeypatch.setattr(server.oracles, "available", lambda k: k == "codex")
    codex_calls = []

    async def fake_codex(question, context="", **kw):
        codex_calls.append(question)
        return OracleResult("ok", text="GPT MERGED", model="gpt-5.6-sol")

    monkeypatch.setattr(server.codex, "run", fake_codex)
    out = _run(server._handle_atlas_council(
        {"question": "How does routing work?",
         "models": ["zai-org/glm-5.2", "deepseek-ai/deepseek-v4-pro"]}
    ))
    assert out["status"] == "ok" and out["answer"] == "GPT MERGED"
    assert out["synthesis"]["requested"] == "codex" and out["synthesis"]["used"] == "codex"
    assert "local codex CLI" in out["synthesis"]["note"]
    assert len(codex_calls) == 1 and keys  # panel fanned out, codex adjudicated


def test_atlas_council_falls_back_to_atlas_hosted_gpt(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    _stub_panel(monkeypatch)
    # codex CLI missing; atlas tokens stay available (key is set)
    monkeypatch.setattr(
        server.oracles, "available",
        lambda k: k != "codex" and server.oracles.atlas_model(k) is not None,
    )
    atlas_calls = []

    async def fake_atlas(model, question, context="", *, effort=None, timeout=None):
        atlas_calls.append(model)
        return OracleResult("ok", text="HOSTED GPT MERGED", model=model)

    monkeypatch.setattr(server.atlas, "run", fake_atlas)
    out = _run(server._handle_atlas_council(
        {"question": "How does routing work?",
         "models": ["zai-org/glm-5.2", "deepseek-ai/deepseek-v4-pro"]}
    ))
    assert out["status"] == "ok" and out["answer"] == "HOSTED GPT MERGED"
    assert out["synthesis"]["used"] == "atlas:openai/gpt-5.6-sol"
    assert "Atlas-hosted" in out["synthesis"]["note"]
    assert atlas_calls == ["openai/gpt-5.6-sol"]


def test_atlas_council_persisted_synthesizer_wins_over_ladder(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    _stub_panel(monkeypatch)
    monkeypatch.setattr(server.atlas, "synthesizer_token", lambda: "fable")
    monkeypatch.setattr(server.oracles, "available", lambda k: True)  # codex would win otherwise
    out = _run(server._handle_atlas_council(
        {"question": "How does routing work?",
         "models": ["zai-org/glm-5.2", "deepseek-ai/deepseek-v4-pro"]}
    ))
    assert out["status"] == "ok"
    assert out["synthesis"]["used"] == "fable"
    assert out["synthesis"]["note"] == "persisted atlas_synthesizer config"


def test_configure_atlas_council_persists(monkeypatch):
    out = server._handle_configure_atlas(
        {"models": ["atlas:zai-org/glm-5.2", "deepseek-ai/deepseek-v4-pro", "zai-org/GLM-5.2"]}
    )
    assert out["status"] == "ok"
    # prefix stripped, case folded, dupes collapsed
    assert out["atlas_council"] == ["zai-org/glm-5.2", "deepseek-ai/deepseek-v4-pro"]
    # persisted so council_models() now returns the saved set
    assert server.atlas.council_models() == ["zai-org/glm-5.2", "deepseek-ai/deepseek-v4-pro"]


def test_configure_atlas_synthesizer_persists_resolved_token(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_ATLAS_SYNTHESIZER", raising=False)
    out = server._handle_configure_atlas({"synthesizer": "gpt"})
    assert out["status"] == "ok" and out["synthesizer"] == "codex"  # alias resolved
    out = server._handle_configure_atlas({"synthesizer": "openai/gpt-5.6-sol"})
    assert out["synthesizer"] == "atlas:openai/gpt-5.6-sol"  # bare atlas id prefixed
    assert server.atlas.synthesizer_token() == "atlas:openai/gpt-5.6-sol"
    # bare mixed-case atlas id keeps its casing (Atlas ids are case-sensitive)
    out = server._handle_configure_atlas({"synthesizer": "deepseek-ai/DeepSeek-V3.1-Terminus"})
    assert out["synthesizer"] == "atlas:deepseek-ai/DeepSeek-V3.1-Terminus"


def test_configure_atlas_rejects_bad_args():
    assert server._handle_configure_atlas({})["kind"] == "bad_args"
    assert server._handle_configure_atlas({"models": []})["kind"] == "bad_args"
    assert server._handle_configure_atlas({"synthesizer": "gpt-4"})["kind"] == "bad_args"


def test_new_tools_registered():
    assert "ask_atlas_council" in server._TOOL_SCHEMAS
    assert "configure_atlas_council" in server._TOOL_SCHEMAS
