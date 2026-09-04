"""ask_fable sequential chain handler — ordered relay, roles, skip/fallback, drift.

Drives ``_handle_chain`` / ``_chain`` with guard, fable, and minimax stubbed and
ASK_FABLE_QUIET=1 so the console reporter stays silent. No model is called.
"""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.server as server
from ask_fable.fable import fable_model
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _quiet_and_no_audit(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


def _run(coro):
    return asyncio.run(coro)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def _sc(rec: str, conf: str = "medium") -> str:
    """A valid trailing sidecar block carrying one recommendation."""
    return (
        f'\n\n```json-sidecar\n{{"sidecar_version": 1, "recommendation": "{rec}", '
        f'"confidence": "{conf}", "needs_context": []}}\n```'
    )


def test_chain_threads_prior_work_and_assigns_roles(monkeypatch):
    _allow(monkeypatch)
    fable_prompts, mmx_prompts = [], []

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        fable_prompts.append(question)
        if "FINAL STAGE" in question:
            return OracleResult("ok", text="FINAL ANSWER")
        return OracleResult("ok", text="fable draft", thinking="fable-think")

    async def fake_minimax(question, context="", **kw):
        mmx_prompts.append(question)
        return OracleResult("ok", text="minimax critique", model="MiniMax-M3", thinking="m3-think")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)

    out = _run(server._handle_chain({"question": "How does dispatch work?", "pipeline": "fable > minimax > fable"}))

    assert out["status"] == "ok" and out["mode"] == "chain"
    assert out["answer"] == "FINAL ANSWER" and out["answered_by"] == fable_model()
    assert out["answered"] == 3 and out["requested"] == 3
    assert [s["role"] for s in out["stages"]] == ["drafter", "critic", "synthesize"]
    assert out["pipeline"] == [fable_model(), "MiniMax-M3", fable_model()]

    # stage 2 (critic) sees the immediately-preceding draft, framed as untrusted peer input
    critic_prompt = mmx_prompts[0]
    assert "UNTRUSTED PEER INPUT" in critic_prompt and "fable draft" in critic_prompt
    # stage 3 (synthesize) sees ALL prior stages as anonymized peers
    synth_prompt = fable_prompts[-1]
    assert "FINAL STAGE" in synth_prompt
    assert "[STAGE 1]" in synth_prompt and "[STAGE 2]" in synth_prompt
    assert "minimax critique" in synth_prompt


def test_chain_skips_failed_stage_and_reassigns_drafter(monkeypatch):
    _allow(monkeypatch)

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        # this fable is stage 2, but stage 1 refused so it should be framed as the DRAFTER
        assert "DRAFTER" in question
        return OracleResult("ok", text="recovered answer")

    async def fake_minimax(question, context="", **kw):
        return OracleResult("refused", text="not about code", model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)

    out = _run(server._handle_chain({"question": "Trace routing here.", "pipeline": "m3 > fable"}))

    assert out["status"] == "ok"
    assert out["answer"] == "recovered answer" and out["answered"] == 1
    assert out["stages"][0]["status"] == "refused"
    assert out["stages"][1]["role"] == "drafter"


def test_chain_final_failure_falls_back_to_fable_synthesis(monkeypatch):
    _allow(monkeypatch)

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        if system_prompt is not None:  # the fallback synthesis turn
            return OracleResult("ok", text="FALLBACK SYNTH")
        return OracleResult("ok", text="fable draft", thinking="ft")

    async def fake_minimax(question, context="", **kw):
        return OracleResult("error", kind="timeout", text="slow", model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)

    out = _run(server._handle_chain({"question": "Explain the loop.", "pipeline": "fable > minimax"}))

    assert out["status"] == "ok" and out["answer"] == "FALLBACK SYNTH"
    assert out["answered_by"] == fable_model() and "fallback" in out
    assert out["answered"] == 1  # only the fable drafter survived


def test_chain_surfaces_recommendation_drift(monkeypatch):
    _allow(monkeypatch)

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        # final synthesize stage reverses the drafter: reject
        return OracleResult("ok", text="final, reversed" + _sc("reject"))

    async def fake_minimax(question, context="", **kw):
        return OracleResult("ok", text="draft, apply" + _sc("apply"), model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)

    out = _run(server._handle_chain({"question": "Should I apply this refactor?", "pipeline": "m3 > fable"}))

    assert out["status"] == "ok"
    assert out["recommendation_drift"] == ["apply", "reject"]
    assert out["material_drift"] is True
    # the sidecar block is stripped from the surfaced answer
    assert "json-sidecar" not in out["answer"] and out["answer"] == "final, reversed"


def test_return_thinking_flag_adds_capped_excerpt(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_RETURN_THINKING", "1")
    monkeypatch.setenv("ASK_FABLE_THINKING_CHARS", "20")

    async def fake_minimax(question, context="", **kw):
        return OracleResult("ok", text="draft", model="MiniMax-M3", thinking="x" * 100)

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="final", thinking="short trace")

    monkeypatch.setattr(server.minimax, "run", fake_minimax)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    out = _run(server._handle_chain({"question": "Design it?", "pipeline": "m3 > fable"}))
    # final stage's trace is surfaced, capped to 20 chars + ellipsis
    assert "thinking" in out and out["thinking"] == "short trace"


def test_return_thinking_off_by_default(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.delenv("ASK_FABLE_RETURN_THINKING", raising=False)

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="answer", thinking="secret reasoning")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    out = _run(server._handle_chain({"question": "One stage?", "pipeline": "fable"}))
    assert "thinking" not in out  # lean by default; trace only on disk


def test_chain_none_answered_refuses(monkeypatch):
    _allow(monkeypatch)

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("refused", text="off scope")

    async def fake_minimax(question, context="", **kw):
        return OracleResult("refused", text="off scope", model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)
    out = _run(server._handle_chain({"question": "what editor is best", "pipeline": "m3 > fable"}))
    assert out["status"] == "refused" and out["stage"] == "model"
