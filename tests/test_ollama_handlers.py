"""ask_fable ollama tool handlers — ask_ollama, ask_ollama_council, and a mixed
ask_council that includes an ollama:<model> token. Guard + backends stubbed."""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _quiet_and_no_audit(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


def _run(coro):
    return asyncio.run(coro)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def test_ask_ollama_ok(monkeypatch):
    _allow(monkeypatch)
    seen = {}

    async def fake_run(model, question, context=""):
        seen["model"] = model
        return OracleResult("ok", text="cloud answer", model=model, thinking="th")

    monkeypatch.setattr(server.ollama, "run", fake_run)
    out = _run(server._handle_ollama(
        {"question": "How does dispatch work?", "model": "kimi-k2.7-code:cloud"}
    ))
    assert out == {"status": "ok", "model": "kimi-k2.7-code:cloud", "answer": "cloud answer",
                   "sidecar": None, "missing_sidecar": True}
    assert seen["model"] == "kimi-k2.7-code:cloud"


def test_ask_ollama_uses_default_model(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.ollama, "default_model", lambda: "gpt-oss:120b-cloud")

    async def fake_run(model, question, context=""):
        return OracleResult("ok", text="a", model=model)

    monkeypatch.setattr(server.ollama, "run", fake_run)
    out = _run(server._handle_ollama({"question": "trace the router here"}))
    assert out["model"] == "gpt-oss:120b-cloud"


def test_ask_ollama_guard_denied(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (False, "prohibited_x"))
    out = _run(server._handle_ollama({"question": "blocked", "model": "x:cloud"}))
    assert out == {"status": "refused", "stage": "guard", "reason": "prohibited_x"}


def test_ask_ollama_council_synthesizes(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_OLLAMA_API_KEY", "k")
    keys = []

    async def fake_oracle_run(key, question, context=""):
        keys.append(key)
        return OracleResult("ok", key=key, text=f"{key} ans", model=server.oracles.label(key))

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="MERGED cloud")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    out = _run(server._handle_ollama_council(
        {"question": "How does routing work?", "models": ["kimi:cloud", "gpt-oss:120b-cloud"]}
    ))
    assert out["status"] == "ok" and out["answer"] == "MERGED cloud"
    # bare model ids get normalized to ollama:<model> tokens
    assert set(keys) == {"ollama:kimi:cloud", "ollama:gpt-oss:120b-cloud"}
    assert set(out["sources"]) == {"ollama:kimi:cloud", "ollama:gpt-oss:120b-cloud"}


def test_ask_ollama_council_defaults_to_configured_set(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_OLLAMA_API_KEY", "k")
    monkeypatch.setattr(server.ollama, "council_models", lambda: ["nemotron-3-ultra:cloud", "qwen3-coder:480b-cloud"])
    keys = []

    async def fake_oracle_run(key, question, context=""):
        keys.append(key)
        return OracleResult("ok", key=key, text=f"{key} ans", model=server.oracles.label(key))

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="MERGED")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    # models omitted -> falls back to the configured council set
    out = _run(server._handle_ollama_council({"question": "How does routing work here?"}))
    assert out["status"] == "ok"
    assert set(keys) == {"ollama:nemotron-3-ultra:cloud", "ollama:qwen3-coder:480b-cloud"}


def test_ask_ollama_council_no_models(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.ollama, "council_models", lambda: [])
    out = _run(server._handle_ollama_council({"question": "trace the router please", "models": []}))
    assert out["status"] == "error" and out["kind"] == "no_models"


def test_list_ollama_models_reports_catalog(monkeypatch):
    monkeypatch.setattr(
        server.ollama, "catalog",
        lambda: {"cloud": ["glm-5.2:cloud", "minimax-m3:cloud"],
                 "local": ["gpt-oss:120b-cloud"], "cloud_ok": True},
    )
    out = _run(server._handle_list_ollama({"refresh": True}))
    assert out["status"] == "ok"
    assert out["cloud_catalog_ok"] is True
    assert out["available_cloud"] == ["glm-5.2:cloud", "minimax-m3:cloud"]
    assert out["pulled_local"] == ["gpt-oss:120b-cloud"]
    assert out["configured_council"] == server.ollama.council_models()


def test_list_ollama_models_no_refresh_skips_network(monkeypatch):
    def _boom():
        raise AssertionError("catalog() must not be called when refresh=false")

    monkeypatch.setattr(server.ollama, "catalog", _boom)
    out = _run(server._handle_list_ollama({"refresh": False}))
    assert out["status"] == "ok" and out["available_cloud"] == []


def test_configure_ollama_council_persists(monkeypatch):
    out = server._handle_configure_ollama(
        {"models": ["ollama:minimax-m3:cloud", "glm-5.2", "minimax-m3:cloud"]}
    )
    assert out["status"] == "ok"
    # prefix stripped, bare 'glm-5.2' normalized to ':cloud', dupes collapsed
    assert out["ollama_council"] == ["minimax-m3:cloud", "glm-5.2:cloud"]
    # persisted so council_models() now returns the saved set
    assert server.ollama.council_models() == ["minimax-m3:cloud", "glm-5.2:cloud"]


def test_configure_ollama_default_model(monkeypatch):
    out = server._handle_configure_ollama({"default_model": "gpt-oss:120b"})
    assert out["status"] == "ok"
    # bare tagged name normalized to the daemon -cloud id
    assert out["default_model"] == "gpt-oss:120b-cloud"


def test_configure_ollama_rejects_empty(monkeypatch):
    assert server._handle_configure_ollama({})["kind"] == "bad_args"
    assert server._handle_configure_ollama({"models": []})["kind"] == "bad_args"


def test_council_mixes_ollama_token(monkeypatch):
    _allow(monkeypatch)
    keys = []

    async def fake_oracle_run(key, question, context=""):
        keys.append(key)
        return OracleResult("ok", key=key, text=f"{key} ans", model=server.oracles.label(key))

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="MERGED")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    out = _run(server._handle_council(
        {"question": "How does routing work?", "models": ["fable", "ollama:kimi:cloud"]}
    ))
    assert out["status"] == "ok"
    assert set(keys) == {"fable", "ollama:kimi:cloud"}
    assert set(out["sources"]) == {"fable", "ollama:kimi:cloud"}
