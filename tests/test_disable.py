"""Operator denylist — turn oracles/providers OFF at runtime.

Two layers: the resolver in `oracles` (env/config precedence, key+alias+provider
matching, honored by `available` and `run`) and the `configure_disabled` tool
(persist/report the list). The config file is auto-isolated per test by conftest,
so each test starts with an empty denylist. No model is ever called.
"""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.config as cfg
import ask_fable.oracles as oracles
import ask_fable.server as server
from ask_fable.oracle_common import OracleResult
from ask_fable.sessions import SessionStore


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.delenv(oracles.DISABLED_KEY, raising=False)


# --- resolver -------------------------------------------------------------


def test_env_denylist_parses_and_normalizes(monkeypatch):
    # comma/space separated, alias-folded (m3 -> minimax), case-insensitive
    monkeypatch.setenv(oracles.DISABLED_KEY, "atlas, M3  grok")
    assert oracles.disabled_tokens() == frozenset({"atlas", "minimax", "grok"})
    assert oracles.is_disabled("grok") is True
    assert oracles.is_disabled("m3") is True and oracles.is_disabled("minimax") is True
    assert oracles.is_disabled("fable") is False


def test_provider_name_disables_every_gateway_token(monkeypatch):
    monkeypatch.setenv(oracles.DISABLED_KEY, "atlas")
    assert oracles.is_disabled("atlas:xai/grok-4.6") is True
    assert oracles.is_disabled("atlas:openai/gpt-5.6-sol") is True
    # a different gateway is untouched
    assert oracles.is_disabled("openrouter:anthropic/claude-opus-5") is False
    assert oracles.available("atlas:zai-org/glm-5.2") is False


def test_config_wins_over_env(monkeypatch):
    monkeypatch.setenv(oracles.DISABLED_KEY, "grok")
    cfg.save({oracles.DISABLED_KEY: ["atlas"]})
    # config replaces env entirely (not a union), matching config.setting precedence
    assert oracles.disabled_tokens() == frozenset({"atlas"})
    assert oracles.is_disabled("grok") is False
    assert oracles.is_disabled("atlas") is True


def test_empty_denylist_disables_nothing(monkeypatch):
    assert oracles.disabled_tokens() == frozenset()
    assert oracles.is_disabled("atlas:x/y") is False
    assert oracles.available("fable") is True


# --- run() gate -----------------------------------------------------------


def test_run_fails_closed_with_disabled_kind(monkeypatch):
    monkeypatch.setenv(oracles.DISABLED_KEY, "grok")
    res = _run(oracles.run("grok", "q"))
    assert res.status == "error" and res.kind == "disabled"
    assert "disabled by the operator" in res.text


def test_multi_turn_tools_honor_the_denylist(monkeypatch):
    """`ask`/`ask_opus5` call the bridge directly, not oracles.run — the gate must
    apply here too, or a disabled fable/opus still answers on its dedicated tool."""

    async def _boom(*a, **k):
        raise AssertionError("bridge ran despite being disabled")

    monkeypatch.setattr(server.opus, "run", _boom)
    monkeypatch.setattr(server.fable, "run", _boom)

    monkeypatch.setenv(oracles.DISABLED_KEY, "opus")
    out = _run(server._handle_opus5(SessionStore(), {"question": "q"}))
    assert out["status"] == "error" and out["kind"] == "disabled"

    monkeypatch.setenv(oracles.DISABLED_KEY, "fable")
    out = _run(server._handle_ask(SessionStore(), {"question": "q"}))
    assert out["status"] == "error" and out["kind"] == "disabled"


# --- paths that reach a bridge beneath run()'s gate ------------------------------

_Q = "How does the retry loop in fetch_user() handle a 503 from the upstream API?"
_SC = (
    '\n```json-sidecar\n{"sidecar_version": 1, "recommendation": "apply", '
    '"confidence": "high", "needs_context": []}\n```'
)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


async def _bridge_ran(*a, **k):
    raise AssertionError("a disabled backend's bridge ran")


def test_run_synthesis_refuses_a_disabled_synthesizer(monkeypatch):
    # run_synthesis dispatches straight to _run_uncached, skipping run()'s gate.
    monkeypatch.setenv(oracles.DISABLED_KEY, "fable")
    monkeypatch.setattr(oracles.fable, "run", _bridge_ran)
    res = _run(oracles.run_synthesis("fable", "reconcile these answers"))
    assert res.status == "error" and res.kind == "disabled"


def test_disabled_member_is_dropped_from_the_council_not_counted(monkeypatch):
    """A disabled Fable used to keep its panel seat (a kind:'disabled' source, so
    quorum 2/3 and never a 'strong' consensus) AND still synthesize the answer.
    Now it is off the panel, reported under `disabled`, and never runs."""
    _allow(monkeypatch)
    monkeypatch.setenv(oracles.DISABLED_KEY, "fable")
    asked = []

    async def fake_run(key, question, context="", **kw):
        asked.append(key)
        return OracleResult("ok", key=key, text=f"{key} says" + _SC, model=f"{key}-model")

    monkeypatch.setattr(server.oracles, "run", fake_run)
    monkeypatch.setattr(server.fable, "run", _bridge_ran)
    out = _run(server._handle_council(
        {"question": _Q, "context": "def fetch_user(): ...",
         "models": ["fable", "minimax", "deepseek"]}
    ))
    assert asked == ["deepseek", "minimax"]
    assert out["disabled"] == ["fable"] and "fable" not in out["sources"]
    assert out["quorum"] == "2/2" and out["degraded"] is False
    assert out["consensus"] == "strong"
    # no enabled synthesizer is left, so the answer is the first panelist's, flagged
    assert out["synthesis"] == {
        "requested": "fable",
        "used": None,
        "fallback": "first_answer",
        "answer_is_unsynthesized": True,
    }


def test_failed_synthesizer_is_not_rescued_by_a_disabled_fable(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv(oracles.DISABLED_KEY, "fable")
    synth_keys = []

    async def fake_run(key, question, context="", **kw):
        return OracleResult("ok", key=key, text=f"{key} says" + _SC, model=f"{key}-model")

    async def fake_synthesis(key, prompt, **kw):
        synth_keys.append(key)
        return OracleResult("error", key=key, kind="timeout", text="codex timed out")

    monkeypatch.setattr(server.oracles, "run", fake_run)
    monkeypatch.setattr(server.oracles, "run_synthesis", fake_synthesis)
    monkeypatch.setattr(server.oracles, "available", lambda k: True)
    out = _run(server._handle_council(
        {"question": _Q, "models": ["minimax", "deepseek"], "synthesizer": "codex"}
    ))
    assert synth_keys == ["codex"]  # the Fable retry never reached a backend
    assert out["status"] == "ok" and out["synthesis"]["used"] is None
    assert out["synthesis"]["answer_is_unsynthesized"] is True


def test_council_of_only_disabled_models_is_a_clean_error(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv(oracles.DISABLED_KEY, "fable, minimax")
    monkeypatch.setattr(server.oracles, "run", _bridge_ran)
    out = _run(server._handle_council({"question": _Q, "models": ["fable", "m3"]}))
    assert out["status"] == "error" and out["kind"] == "disabled"
    assert out["disabled"] == ["fable", "minimax"]


def test_disabled_fable_is_not_the_chain_rescue(monkeypatch):
    # The chain's fallback synthesis called fable.run directly, beneath run()'s gate.
    _allow(monkeypatch)
    monkeypatch.setenv(oracles.DISABLED_KEY, "fable")

    async def fake_run(key, question, context="", **kw):
        if key == "minimax":
            return OracleResult("ok", key=key, text="the draft", model="MiniMax-M3")
        return OracleResult("error", key=key, kind="timeout", text="slow", model=key)

    monkeypatch.setattr(server.oracles, "run", fake_run)
    monkeypatch.setattr(server.fable, "run", _bridge_ran)
    out = _run(server._handle_chain({"question": _Q, "pipeline": "m3 > glm"}))
    assert out["status"] == "ok" and out["answer"] == "the draft"
    assert out["answered_by"] == "MiniMax-M3" and "Fable is disabled" in out["fallback"]


@pytest.mark.parametrize(
    "denied,model",
    [("grok", "grok"), ("gemini", "agy"), ("opus", "opus5"), ("opus5", "opus5")],
)
def test_websearch_honors_the_denylist_for_its_backend(monkeypatch, denied, model):
    # The router drives the grok/agy/Claude bridge directly; oracles.run only ever
    # checked the "websearch" key, so a disabled backend still browsed.
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_ALLOW_WEBSEARCH", "1")
    monkeypatch.setenv(oracles.DISABLED_KEY, denied)
    for bridge in (server.websearch.grok, server.websearch.gemini, server.websearch.fable):
        monkeypatch.setattr(bridge, "run", _bridge_ran)
    out = _run(server._handle_websearch({"question": "latest requests release?", "model": model}))
    assert out["status"] == "error" and out["kind"] == "disabled"


def test_disabling_anthropic_is_flagged_not_silently_ignored(monkeypatch):
    # 'anthropic' maps to no oracle, so the tool must warn rather than store a no-op
    out = server._handle_configure_disabled({"disable": ["anthropic"]})
    assert out["unknown_tokens"] == ["anthropic"] and "note" in out


def test_disabled_atlas_blocks_the_glm_fallback(monkeypatch):
    # glm with no direct key normally borrows Atlas; a disabled Atlas must not be
    # reached indirectly.
    monkeypatch.setenv(oracles.DISABLED_KEY, "atlas")
    monkeypatch.setattr(oracles.anthropic_http, "config_for", lambda k: None)
    monkeypatch.setattr(oracles.atlas, "configured", lambda: True)

    async def _boom(*a, **k):  # would run if the fallback fired
        raise AssertionError("atlas fallback ran despite being disabled")

    monkeypatch.setattr(oracles.atlas, "run", _boom)
    res = _run(oracles.run("glm", "q"))
    assert res.status == "error" and res.kind == "not_configured"


# --- configure_disabled tool ---------------------------------------------


def test_tool_disable_then_enable_roundtrips(monkeypatch):
    out = server._handle_configure_disabled({"disable": ["atlas"]})
    assert out["status"] == "ok" and out["disabled"] == ["atlas"]
    assert oracles.is_disabled("atlas:x/y") is True

    out = server._handle_configure_disabled({"enable": ["atlas"]})
    assert out["status"] == "ok" and out["disabled"] == []
    assert oracles.is_disabled("atlas:x/y") is False


def test_tool_reports_without_mutating(monkeypatch):
    server._handle_configure_disabled({"disable": ["grok", "atlas"]})
    out = server._handle_configure_disabled({})  # no mutating arg → report only
    assert out["status"] == "ok" and out["disabled"] == ["atlas", "grok"]
    assert "saved_to" not in out  # nothing written


def test_tool_set_replaces_and_folds_aliases(monkeypatch):
    server._handle_configure_disabled({"disable": ["grok"]})
    out = server._handle_configure_disabled({"set": ["m3", "atlas"]})
    # m3 stored as its canonical key; grok replaced away
    assert out["disabled"] == ["atlas", "minimax"]
    assert "unknown_tokens" not in out


def test_tool_flags_unknown_tokens_but_still_stores(monkeypatch):
    out = server._handle_configure_disabled({"disable": ["notathing"]})
    assert out["disabled"] == ["notathing"]
    assert out["unknown_tokens"] == ["notathing"] and "note" in out


def test_tool_disable_merges_onto_the_effective_set(monkeypatch):
    # `disable` adds to what's already OFF (env included), so an env entry is kept
    # and baked into the persisted config alongside the new one.
    monkeypatch.setenv(oracles.DISABLED_KEY, "grok")
    out = server._handle_configure_disabled({"disable": ["atlas"]})
    assert out["disabled"] == ["atlas", "grok"]
    assert oracles.is_disabled("grok") is True and oracles.is_disabled("atlas") is True


def test_tool_set_empty_clears_config_and_falls_back_to_env(monkeypatch):
    monkeypatch.setenv(oracles.DISABLED_KEY, "grok")
    server._handle_configure_disabled({"disable": ["atlas"]})
    out = server._handle_configure_disabled({"set": []})  # removes the config key
    # with the config key gone, the env denylist is the source again
    assert out["disabled"] == ["grok"]
    assert oracles.is_disabled("grok") is True


def test_tool_enable_of_the_last_env_entry_sticks(monkeypatch):
    # Emptying the list used to REMOVE the config key, so the env denylist applied
    # again and the call reported `disabled: ["grok"]` — a silent no-op.
    monkeypatch.setenv(oracles.DISABLED_KEY, "grok")
    out = server._handle_configure_disabled({"enable": ["grok"]})
    assert out["status"] == "ok" and out["disabled"] == []
    assert oracles.is_disabled("grok") is False
    assert cfg.load()[oracles.DISABLED_KEY] == []  # an explicit override, not a removal


def test_empty_config_list_overrides_the_env_denylist(monkeypatch):
    monkeypatch.setenv(oracles.DISABLED_KEY, "grok atlas")
    cfg.save({oracles.DISABLED_KEY: []})
    assert oracles.disabled_tokens() == frozenset()
    # other list settings still read an empty array as "unset"
    assert cfg.get_list(oracles.DISABLED_KEY) is None
