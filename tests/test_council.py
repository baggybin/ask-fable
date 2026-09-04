"""ask_fable council handler — fan-out, synthesis, degrade paths.

Drives ``_handle_council`` with guard, fable, and minimax stubbed, and
ASK_FABLE_QUIET=1 so the console reporter stays silent. No model is called.
"""

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


def _stub(monkeypatch, fable_res, mmx_res):
    calls = {"fable": [], "minimax": [], "fable_systems": []}

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        calls["fable"].append(question)
        calls["fable_systems"].append(system_prompt)
        # First fable call -> the oracle answer; a later call with a synth system
        # prompt -> the synthesis result (passed as a 3rd element when provided).
        if system_prompt is not None and len(fable_res) > 2:
            return fable_res[2]
        return fable_res[0]

    async def fake_minimax(question, context="", **kw):
        calls["minimax"].append(question)
        return mmx_res

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)
    return calls


def test_guard_denied_calls_no_model(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (False, "prohibited_x"))
    calls = _stub(monkeypatch, (OracleResult("ok", text="x"),), OracleResult("ok", text="y"))
    out = _run(server._handle_council({"question": "some blocked question here"}))
    assert out == {"status": "refused", "stage": "guard", "reason": "prohibited_x"}
    assert calls["fable"] == [] and calls["minimax"] == []


def test_both_ok_synthesizes(monkeypatch):
    _allow(monkeypatch)
    calls = _stub(
        monkeypatch,
        (
            OracleResult("ok", text="fable says A", session_id="sid"),
            None,
            OracleResult("ok", text="MERGED answer"),  # synthesis turn result
        ),
        OracleResult("ok", text="minimax says B", model="MiniMax-M3"),
    )
    out = _run(server._handle_council({"question": "How does routing work here?"}))
    assert out["status"] == "ok" and out["mode"] == "council"
    assert out["answer"] == "MERGED answer"
    assert out["synthesizer"] == server.fable.fable_model()
    # both raw answers are preserved under sources
    assert out["sources"]["fable"]["answer"] == "fable says A"
    assert out["sources"]["minimax"]["answer"] == "minimax says B"
    # synthesis used the synth system prompt (a 2nd fable call with a prompt override)
    assert any(s is not None for s in calls["fable_systems"])
    # structured envelope surfaces quorum so a caller can see it wasn't degraded
    assert out["quorum"] == "2/2" and out["degraded"] is False
    assert set(out["effective_models"]) == {server.fable.fable_model(), "MiniMax-M3"}
    assert out["confidence"] == "medium" and "recommended_next_action" in out


def test_lone_answer_envelope_flags_degraded(monkeypatch):
    _allow(monkeypatch)
    _stub(
        monkeypatch,
        (OracleResult("ok", text="only fable answered"),),
        OracleResult("error", kind="binary_missing", text="no mmx", model="MiniMax-M3"),
    )
    out = _run(server._handle_council({"question": "Trace dispatch in this module."}))
    # 1 of 2 answered -> degraded, low confidence, single-model warning
    assert out["quorum"] == "1/2" and out["degraded"] is True
    assert out["confidence"] == "low"
    assert "single-model" in out["recommended_next_action"]


def test_one_ok_returns_lone_answer_no_synth(monkeypatch):
    _allow(monkeypatch)
    calls = _stub(
        monkeypatch,
        (OracleResult("ok", text="only fable answered"),),
        OracleResult("error", kind="binary_missing", text="no mmx", model="MiniMax-M3"),
    )
    out = _run(server._handle_council({"question": "Trace dispatch in this module."}))
    assert out["status"] == "ok" and out["synthesizer"] is None
    assert out["answer"] == "only fable answered"
    assert out["answered_by"] == server.fable.fable_model()
    assert out["sources"]["minimax"]["kind"] == "binary_missing"
    # only the two oracle calls happened — no synthesis turn
    assert len(calls["fable"]) == 1


def test_both_refused_refuses(monkeypatch):
    _allow(monkeypatch)
    _stub(
        monkeypatch,
        (OracleResult("refused", text="not about code"),),
        OracleResult("refused", text="off scope", model="MiniMax-M3"),
    )
    out = _run(server._handle_council({"question": "what is the best editor really"}))
    assert out["status"] == "refused" and out["stage"] == "model"
    assert out["reason"] == "not about code"
    assert out["sources"]["minimax"]["reason"] == "off scope"


def test_both_error_returns_error(monkeypatch):
    _allow(monkeypatch)
    _stub(
        monkeypatch,
        (OracleResult("error", kind="timeout", text="Fable timed out"),),
        OracleResult("error", kind="timeout", text="MiniMax timed out", model="MiniMax-M3"),
    )
    out = _run(server._handle_council({"question": "Where is the router defined here?"}))
    assert out["status"] == "error" and out["kind"] == "timeout"
    assert "fable" in out["sources"] and "minimax" in out["sources"]


def test_models_param_four_way_synthesizes(monkeypatch):
    _allow(monkeypatch)

    answers = {
        "fable": OracleResult("ok", key="fable", text="fable A", model="claude-fable-5"),
        "minimax": OracleResult("ok", key="minimax", text="mmx B", model="MiniMax-M3"),
        "glm": OracleResult("ok", key="glm", text="glm C", model="glm-5.2"),
        "deepseek": OracleResult("ok", key="deepseek", text="ds D", model="deepseek-v4-pro"),
    }
    seen = {"oracle_keys": [], "synth_answers": None}

    async def fake_oracle_run(key, question, context=""):
        seen["oracle_keys"].append(key)
        return answers[key]

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        seen["synth_answers"] = question  # the composed synth prompt
        return OracleResult("ok", text="MERGED four")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)

    out = _run(server._handle_council(
        {"question": "How does routing work?", "models": ["fable", "minimax", "glm", "deepseek"]}
    ))
    assert out["status"] == "ok" and out["answer"] == "MERGED four"
    assert set(seen["oracle_keys"]) == {"fable", "minimax", "glm", "deepseek"}
    assert set(out["sources"]) == {"fable", "minimax", "glm", "deepseek"}
    # every model's answer was fed to the synthesizer
    for frag in ("fable A", "mmx B", "glm C", "ds D"):
        assert frag in seen["synth_answers"]


def test_unknown_model_ignored_defaults_when_all_unknown(monkeypatch):
    _allow(monkeypatch)

    async def fake_oracle_run(key, question, context=""):
        return OracleResult("ok", key=key, text=f"{key} ans", model=key)

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="merged")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    out = _run(server._handle_council({"question": "q about routing here", "models": ["nope"]}))
    # all-unknown -> falls back to default fable+minimax
    assert set(out["sources"]) == {"fable", "minimax"}


def test_tier_middle_and_full_expand(monkeypatch):
    _allow(monkeypatch)

    seen = {"keys": []}

    async def fake_oracle_run(key, question, context=""):
        seen["keys"].append(key)
        return OracleResult("ok", key=key, text=f"{key} ans", model=key)

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="merged")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.ollama, "council_models", lambda: ["nemotron-3-ultra:cloud"])

    # middle -> all KNOWN members, fanned out cheap-first
    seen["keys"] = []
    _run(server._handle_council({"question": "How does routing work here?", "tier": "middle"}))
    assert seen["keys"] == [
        "fable", "opus", "deepseek", "minimax", "glm", "gemini", "codex", "grok", "kimi",
    ]

    # full -> middle + configured ollama models (as ollama:<model> tokens)
    seen["keys"] = []
    _run(server._handle_council({"question": "How does routing work here?", "tier": "full"}))
    assert set(seen["keys"]) == {
        "fable", "opus", "minimax", "glm", "deepseek", "gemini", "codex", "grok", "kimi",
        "ollama:nemotron-3-ultra:cloud",
    }

    # explicit models overrides tier
    seen["keys"] = []
    _run(server._handle_council(
        {"question": "How does routing work here?", "tier": "full", "models": ["fable"]}
    ))
    assert set(seen["keys"]) == {"fable"}


def test_default_council_grows_deepseek_when_key_set(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_DEEPSEEK_API_KEY", "k")

    seen = {"keys": []}

    async def fake_oracle_run(key, question, context=""):
        seen["keys"].append(key)
        return OracleResult("ok", key=key, text=f"{key} ans", model=key)

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="merged")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)

    # no models/tier -> availability-aware default, deepseek before minimax
    out = _run(server._handle_council({"question": "How does routing work here?"}))
    assert seen["keys"] == ["fable", "deepseek", "minimax"]
    assert set(out["sources"]) == {"fable", "deepseek", "minimax"}


def test_synth_failure_falls_back_to_fable(monkeypatch):
    _allow(monkeypatch)
    _stub(
        monkeypatch,
        (
            OracleResult("ok", text="fable answer stands"),
            None,
            OracleResult("error", kind="timeout", text="synth timed out"),
        ),
        OracleResult("ok", text="minimax answer", model="MiniMax-M3"),
    )
    out = _run(server._handle_council({"question": "How does the module route requests?"}))
    assert out["status"] == "ok" and out["synthesizer"] is None
    assert out["answer"] == "fable answer stands"


def test_synthesis_passes_thinking_traces(monkeypatch):
    _allow(monkeypatch)

    calls = []
    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        if system_prompt is not None:
            calls.append(question)
            return OracleResult("ok", text="synthesis result")
        return OracleResult("ok", text="fable answer", thinking="fable logic")

    async def fake_minimax(question, context="", **kw):
        return OracleResult("ok", text="minimax answer", model="MiniMax-M3", thinking="minimax logic")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)

    out = _run(server._handle_council({"question": "Explain concurrency."}))
    assert out["status"] == "ok"
    assert out["answer"] == "synthesis result"
    assert len(calls) == 1
    synth_prompt = calls[0]
    assert "[EXPERT A] thinking process:\nminimax logic" in synth_prompt
    assert "[EXPERT A] final answer:\nminimax answer" in synth_prompt
    assert "[EXPERT B] thinking process:\nfable logic" in synth_prompt
    assert "[EXPERT B] final answer:\nfable answer" in synth_prompt
