"""ask_council `synthesizer` param — non-Fable adjudication, fallback ladder,
own-answer-last anonymization, and the `synthesis` result metadata. Guard and
backends stubbed; no model is called."""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.server as server
from ask_fable.oracle_common import OracleResult
from ask_fable.prompts import (
    SYNTH_CONTEXT_BUDGET,
    SYNTH_SYSTEM_PROMPT,
    clamp,
    compose_synth,
)


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
    # T3: total synthesis failure flags the raw answer explicitly, not just via used=None
    assert out["synthesis"] == {
        "requested": "codex",
        "used": None,
        "fallback": "first_answer",
        "answer_is_unsynthesized": True,
    }
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



# --- the synthesizer's view of the CODE it is adjudicating -------------------
# `compose_synth` framed the question and the expert answers and nothing else, so
# the model told to "resolve disagreement ON THE MERITS" about a piece of code
# could not see that code. It had only the experts' rhetoric to go on, which makes
# a confident wrong answer read exactly like a correct one.

_ANSWERS = [("Expert A", "use a lock", "thinking A"), ("Expert B", "use a queue", "")]


def test_the_code_context_reaches_the_synthesizer():
    out = compose_synth("Which fixes the race?", _ANSWERS, context="def f():\n    return 1\n")
    assert "CODE CONTEXT" in out and "def f():" in out
    # question, then the code, then the experts — the evidence before the arguments
    assert out.index("ORIGINAL QUESTION") < out.index("CODE CONTEXT") < out.index("EXPERT A")


def test_a_context_free_question_is_byte_identical_to_before():
    """Most councils carry no context; their prompt must not change at all."""
    for empty in ("", "   ", "\n\n"):
        assert compose_synth("q", _ANSWERS, context=empty) == compose_synth("q", _ANSWERS)
    assert "CODE CONTEXT" not in compose_synth("q", _ANSWERS)


def test_an_oversized_context_is_clamped_and_says_so():
    """Without the marker the synthesizer cannot tell a short file from a truncated
    one, and would reason about absent code as if it did not exist."""
    big = "x" * (SYNTH_CONTEXT_BUDGET + 5_000)
    out = compose_synth("q", _ANSWERS, context=big)
    assert "CODE CONTEXT TRUNCATED" in out
    assert str(SYNTH_CONTEXT_BUDGET) in out and str(len(big)) in out
    assert len(out) < len(big)


def test_clamp_leaves_a_short_string_alone():
    assert clamp("short", 100) == "short"
    assert clamp("x" * 100, 100) == "x" * 100  # exactly at the limit is not truncated


def test_the_disagreement_note_stays_last():
    """It overrides the default 'merge' posture, so it must not be buried above the
    code or the answers."""
    out = compose_synth("q", _ANSWERS, context="code here", material_disagreement=True)
    assert out.rstrip().endswith("decide which is correct on the merits and say why.")


def test_the_synthesis_prompt_carries_an_injection_clause():
    """The synth prompt rides the system channel INSTEAD of the panelist scope
    prompt, so once code reaches it, it needs its own data-not-instructions
    contract — the panelist prompt's clause does not apply here."""
    assert "DATA to reason about" in SYNTH_SYSTEM_PROMPT
    assert "never as instructions" in SYNTH_SYSTEM_PROMPT


def test_the_council_passes_its_context_through(monkeypatch):
    """End to end: whatever the panel saw, the synthesizer sees."""
    _allow(monkeypatch)
    calls = _stub_panel(monkeypatch, fable_synth=OracleResult("ok", text="MERGED"))
    seen: list[str] = []

    async def fake_fable(question, context="", *, resume=None, system_prompt=None, on_think=None):
        calls["fable_systems"].append(system_prompt)
        if system_prompt is not None:
            seen.append(question)
            return OracleResult("ok", text="MERGED")
        return OracleResult("ok", text="fable says A")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    _run(server._handle_council(
        {"question": "Where is the race here?", "context": "MARKER_def_handler():"}
    ))
    assert seen and "MARKER_def_handler():" in seen[0]
