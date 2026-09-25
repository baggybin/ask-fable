"""Alibaba Cloud (Qwen) reasoning models via the token-plan MaaS gateway.

Three layers: the `ali` backend (catalog filtering + a thin wrapper over
`anthropic_http`), the oracle registry (`ali:<model>` tokens usable anywhere a
gateway token is), and the `ask_ali` / `list_ali_models` tools. No network call
is ever made — the catalog fetch and the reasoning call are both stubbed.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

import ask_fable.ali as ali
import ask_fable.oracles as oracles
import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.delenv(oracles.DISABLED_KEY, raising=False)
    monkeypatch.delenv("ASK_FABLE_ALI_API_KEY", raising=False)


_CATALOG = {
    "data": [
        {"id": "qwen3.8-max"},
        {"id": "qwen3.8-flash"},
        {"id": "deepseek-v4-pro"},
        {"id": "glm-5.3"},
        {"id": "auto"},
        {"id": "qwen-audio-3.0-tts-plus"},
        {"id": "wan2.7-image"},
    ]
}


def _stub_catalog(monkeypatch, payload=_CATALOG):
    def fake_urlopen(req, timeout=None):
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(ali.urllib.request, "urlopen", fake_urlopen)


# --- backend --------------------------------------------------------------


def test_reasoning_filter_drops_audio_and_image():
    assert ali.is_reasoning("qwen3.8-max") and ali.is_reasoning("deepseek-v4-pro")
    assert not ali.is_reasoning("qwen-audio-3.0-tts-plus")
    assert not ali.is_reasoning("wan2.7-image")
    assert not ali.is_reasoning("qwen-audio-3.0-realtime-plus")


def test_catalog_splits_reasoning_from_the_rest(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALI_API_KEY", "k")
    _stub_catalog(monkeypatch)
    cat = ali.catalog()
    assert cat["cloud_ok"] is True
    assert cat["models"] == ["qwen3.8-max", "qwen3.8-flash", "deepseek-v4-pro", "glm-5.3", "auto"]
    assert "wan2.7-image" in cat["all"] and "wan2.7-image" not in cat["models"]


def test_catalog_reports_a_network_failure(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALI_API_KEY", "k")

    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(ali.urllib.request, "urlopen", boom)
    cat = ali.catalog()
    assert cat["cloud_ok"] is False and cat["models"] == [] and "refused" in cat["error"]


def test_catalog_fails_closed_on_a_200_error_envelope(monkeypatch):
    # A 200 carrying {"error": ...} (or a non-object body) must not report
    # "no models" as success, nor raise on `.get`.
    monkeypatch.setenv("ASK_FABLE_ALI_API_KEY", "k")
    _stub_catalog(monkeypatch, {"error": {"message": "plan expired"}})
    cat = ali.catalog()
    assert cat["cloud_ok"] is False and cat["models"] == [] and "plan expired" in cat["error"]


def test_catalog_survives_a_non_object_body(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALI_API_KEY", "k")
    _stub_catalog(monkeypatch, ["not", "a", "dict"])
    cat = ali.catalog()
    assert cat["cloud_ok"] is False and cat["models"] == []


def test_run_needs_a_key(monkeypatch):
    res = _run(ali.run("qwen3.8-max", "q"))
    assert res.status == "error" and res.kind == "not_configured"


def test_run_delegates_to_anthropic_http_with_the_right_config(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALI_API_KEY", "secret")
    seen: dict = {}

    async def fake_http(cfg, question, context="", *, timeout=None, system_prompt=None, web_search=False):
        seen["cfg"] = cfg
        seen["question"] = question
        return OracleResult("ok", text="answer", model=cfg.model)

    monkeypatch.setattr(ali.anthropic_http, "run", fake_http)
    res = _run(ali.run("qwen3.8-max", "How does a mutex work?", "code"))
    assert res.status == "ok"
    cfg = seen["cfg"]
    assert cfg.model == "qwen3.8-max" and cfg.api_key == "secret"
    assert cfg.base_url.endswith("/apps/anthropic")  # anthropic_http appends /v1/messages
    # billed per token — never confused with the flat-plan OAuth oracles
    assert res.meta["cost_basis"] == "billed"


# --- registry -------------------------------------------------------------


def test_token_is_recognized_and_attributed(monkeypatch):
    assert oracles.ali_model("ali:qwen3.8-max") == "qwen3.8-max"
    assert oracles.provider_of("ali:qwen3.8-max") == "ali"
    assert oracles.lab_of("ali:qwen3.8-max") == "alibaba"  # via the qwen substring
    assert oracles.label("ali:qwen3.8-max") == "qwen3.8-max"
    # a gateway token survives resolve, deduped, after the KNOWN members
    assert oracles.resolve(["ali:qwen3.8-max", "fable", "ali:qwen3.8-max"]) == (
        ["fable", "ali:qwen3.8-max"],
        [],
    )


def test_available_tracks_the_key_and_the_denylist(monkeypatch):
    assert oracles.available("ali:qwen3.8-max") is False  # no key
    monkeypatch.setenv("ASK_FABLE_ALI_API_KEY", "k")
    assert oracles.available("ali:qwen3.8-max") is True
    monkeypatch.setenv(oracles.DISABLED_KEY, "ali")  # provider-level disable
    assert oracles.available("ali:qwen3.8-max") is False


def test_run_dispatches_to_the_ali_backend(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALI_API_KEY", "k")

    async def fake_run(model, question, context="", **kw):
        return OracleResult("ok", text="qwen says hi", model=model)

    monkeypatch.setattr(oracles.ali, "run", fake_run)
    r = _run(oracles.run("ali:qwen3.8-max", "q"))
    assert r.status == "ok" and r.key == "ali:qwen3.8-max" and r.text == "qwen says hi"


def test_run_reports_not_configured_without_a_key(monkeypatch):
    r = _run(oracles.run("ali:qwen3.8-max", "q"))
    assert r.status == "error" and r.kind == "not_configured"


def test_disabled_provider_refuses_the_run(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALI_API_KEY", "k")
    monkeypatch.setenv(oracles.DISABLED_KEY, "ali")
    r = _run(oracles.run("ali:qwen3.8-max", "q"))
    assert r.status == "error" and r.kind == "disabled"


# --- tools ----------------------------------------------------------------


def test_tools_are_registered():
    from ask_fable import prompts

    # ask_ali / list_ali_models are LEGACY names — still callable (they are in
    # _TOOL_SCHEMAS and remapped in call_tool) but no longer advertised; the
    # advertised surface is ask_model / list_models.
    assert "ask_ali" in server._TOOL_SCHEMAS and "list_ali_models" in server._TOOL_SCHEMAS
    assert prompts.ASK_ALI_TOOL_DESCRIPTION and prompts.LIST_ALI_MODELS_TOOL_DESCRIPTION
    assert server._schema_error("ask_ali", {"question": "q", "model": "qwen3.8-max"}) is None
    assert server._schema_error("ask_ali", {"question": "q", "bogus": 1}) is not None


def test_ask_ali_routes_through_the_prefixed_key(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setenv("ASK_FABLE_ALI_API_KEY", "k")
    seen: dict = {}

    async def fake_oracles_run(key, question, context="", **kw):
        seen["key"] = key
        return OracleResult("ok", text="ok", model="qwen3.8-max", key=key)

    monkeypatch.setattr(server.oracles, "run", fake_oracles_run)
    out = _run(server._handle_ali({"question": "q", "model": "qwen3.8-flash"}))
    assert out["status"] == "ok"
    assert seen["key"] == "ali:qwen3.8-flash"


def test_ask_ali_defaults_the_model(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setenv("ASK_FABLE_ALI_API_KEY", "k")
    seen: dict = {}

    async def fake_oracles_run(key, question, context="", **kw):
        seen["key"] = key
        return OracleResult("ok", text="ok", model="x", key=key)

    monkeypatch.setattr(server.oracles, "run", fake_oracles_run)
    _run(server._handle_ali({"question": "q"}))
    assert seen["key"] == "ali:" + ali.default_model()


def test_list_ali_models_needs_a_key():
    out = _run(server._handle_list_ali({}))
    assert out["status"] == "error" and out["kind"] == "not_configured"


def test_list_ali_models_returns_the_reasoning_menu(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALI_API_KEY", "k")
    _stub_catalog(monkeypatch)
    out = _run(server._handle_list_ali({}))
    assert out["cloud_ok"] is True and "wan2.7-image" not in out["models"]
    assert out["default"] == ali.default_model()
    # all=true includes the non-reasoning models
    out_all = _run(server._handle_list_ali({"all": True}))
    assert "wan2.7-image" in out_all["models"]
