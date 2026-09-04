"""ask_council `synthesizer` param — non-Fable adjudication, fallback ladder,
own-answer-last anonymization, and the `synthesis` result metadata. Guard and
backends stubbed; no model is called."""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.server as server
from ask_fable.oracle_common import OracleResult
from ask_fable.prompts import SYNTH_SYSTEM_PROMPT


@pytest.fixture(autouse=True)
def _quiet_and_no_audit(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


def _run(coro):
    return asyncio.run(coro)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def _stub_panel(monkeypatch, *, fable_synth=None):
    """Stub fable+minimax panelists; fable also answers a synthesis turn with
    ``fable_synth`` when given (else echoes its panel answer)."""
    calls = {"fable_systems": [], "codex": []}

    async def fake_fable(question, context="", *, resume=None, system_prompt=None, on_think=None):
        calls["fable_systems"].append(system_prompt)
        if system_prompt is not None and fable_synth is not None:
            return fable_synth
        return OracleResult("ok", text="fable says A")

    async def fake_minimax(question, context="", **kw):
        return OracleResult("ok", text="minimax says B", model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)
    return calls


def test_codex_synthesizer_dispatches(monkeypatch):
    _allow(monkeypatch)
    calls = _stub_panel(monkeypatch)

    async def fake_codex(question, context="", **kw):
        calls["codex"].append(question)
        return OracleResult("ok", text="CODEX MERGED", model="gpt-5.6-sol")

    monkeypatch.setattr(server.codex, "run", fake_codex)
    monkeypatch.setattr(server.oracles, "available", lambda k: True)
    out = _run(server._handle_council(
        {"question": "How does routing work?", "models": ["fable", "minimax"],
         "synthesizer": "codex"}
    ))
    assert out["status"] == "ok" and out["answer"] == "CODEX MERGED"
    assert out["synthesizer"] == server.oracles.label("codex")
    assert out["synthesis"] == {"requested": "codex", "used": "codex", "fallback": None}
    # the synthesis prompt carried the SYNTH contract (folded in-message) + both answers
    assert len(calls["codex"]) == 1
    assert calls["codex"][0].startswith(SYNTH_SYSTEM_PROMPT)
    assert "fable says A" in calls["codex"][0] and "minimax says B" in calls["codex"][0]
    # fable was only a panelist — it never saw a synth system prompt
    assert all(s is None for s in calls["fable_systems"])


def test_gpt_alias_resolves_to_codex(monkeypatch):
    _allow(monkeypatch)
    calls = _stub_panel(monkeypatch)

    async def fake_codex(question, context="", **kw):
        calls["codex"].append(question)
        return OracleResult("ok", text="MERGED", model="gpt-5.6-sol")

    monkeypatch.setattr(server.codex, "run", fake_codex)
    monkeypatch.setattr(server.oracles, "available", lambda k: True)
    out = _run(server._handle_council(
        {"question": "How does routing work?", "models": ["fable", "minimax"],
         "synthesizer": "gpt"}
    ))
    assert out["synthesis"]["requested"] == "codex" and len(calls["codex"]) == 1


def test_unknown_synthesizer_is_bad_args_before_any_call(monkeypatch):
    _allow(monkeypatch)
    calls = _stub_panel(monkeypatch)
    out = _run(server._handle_council(
        {"question": "How does routing work?", "synthesizer": "gpt-4"}
    ))
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert "unknown synthesizer" in out["detail"]
    assert calls["fable_systems"] == []  # no fan-out happened


def test_default_synthesis_reports_fable_metadata(monkeypatch):
    _allow(monkeypatch)
    _stub_panel(monkeypatch, fable_synth=OracleResult("ok", text="MERGED"))
    out = _run(server._handle_council({"question": "How does routing work?"}))
    assert out["answer"] == "MERGED"
    assert out["synthesizer"] == server.fable.fable_model()
    assert out["synthesis"] == {"requested": "fable", "used": "fable", "fallback": None}


def test_own_answer_last_keys_on_chosen_synthesizer(monkeypatch):
    _allow(monkeypatch)
    _stub_panel(monkeypatch)
    synth_prompts = []

    async def fake_codex(question, context="", **kw):
        if question.startswith(SYNTH_SYSTEM_PROMPT):
            synth_prompts.append(question)
            return OracleResult("ok", text="MERGED", model="gpt-5.6-sol")
        return OracleResult("ok", text="codex says C", model="gpt-5.6-sol")

    monkeypatch.setattr(server.codex, "run", fake_codex)
    monkeypatch.setattr(server.oracles, "available", lambda k: True)
    out = _run(server._handle_council(
        {"question": "How does routing work?", "models": ["fable", "codex"],
         "synthesizer": "codex"}
    ))
    assert out["status"] == "ok" and len(synth_prompts) == 1
    # codex synthesizes, so ITS panel answer is anonymized LAST (Expert B)
    assert "[EXPERT A] final answer:\nfable says A" in synth_prompts[0]
    assert "[EXPERT B] final answer:\ncodex says C" in synth_prompts[0]


def test_failed_synthesizer_falls_back_to_fable(monkeypatch):
    _allow(monkeypatch)
    _stub_panel(monkeypatch, fable_synth=OracleResult("ok", text="FABLE MERGED"))

    async def fake_codex(question, context="", **kw):
        return OracleResult("error", kind="timeout", text="codex timed out")

    monkeypatch.setattr(server.codex, "run", fake_codex)
    monkeypatch.setattr(server.oracles, "available", lambda k: True)
    out = _run(server._handle_council(
        {"question": "How does routing work?", "models": ["fable", "minimax"],
         "synthesizer": "codex"}
    ))
    assert out["status"] == "ok" and out["answer"] == "FABLE MERGED"
    assert out["synthesizer"] == server.fable.fable_model()
    assert out["synthesis"] == {"requested": "codex", "used": "fable", "fallback": "fable"}


def test_both_synthesizers_fail_returns_first_answer(monkeypatch):
    _allow(monkeypatch)
    _stub_panel(monkeypatch, fable_synth=OracleResult("error", kind="timeout", text="synth to"))

    async def fake_codex(question, context="", **kw):
        return OracleResult("error", kind="timeout", text="codex timed out")

    monkeypatch.setattr(server.codex, "run", fake_codex)
    monkeypatch.setattr(server.oracles, "available", lambda k: True)
    out = _run(server._handle_council(
        {"question": "How does routing work?", "models": ["fable", "minimax"],
         "synthesizer": "codex"}
    ))
    assert out["status"] == "ok" and out["synthesizer"] is None
    assert out["answer"] == "fable says A"  # first ok panelist's raw answer
    assert out["synthesis"] == {"requested": "codex", "used": None, "fallback": "first_answer"}
    assert out["confidence"] == "low"


def test_unavailable_synthesizer_skips_straight_to_fable(monkeypatch):
    _allow(monkeypatch)
    _stub_panel(monkeypatch, fable_synth=OracleResult("ok", text="FABLE MERGED"))

    async def boom_codex(question, context="", **kw):
        raise AssertionError("codex.run must not be called when unavailable")

    monkeypatch.setattr(server.codex, "run", boom_codex)
    monkeypatch.setattr(server.oracles, "available", lambda k: k != "codex")
    out = _run(server._handle_council(
        {"question": "How does routing work?", "models": ["fable", "minimax"],
         "synthesizer": "codex"}
    ))
    assert out["status"] == "ok" and out["answer"] == "FABLE MERGED"
    assert out["synthesis"] == {"requested": "codex", "used": "fable", "fallback": "fable"}
