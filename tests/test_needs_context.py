"""PR2: needs_context followup hint + the loop terminator (context_exhausted)."""

from __future__ import annotations

import asyncio
import json

import pytest

import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE", "0")
    monkeypatch.setenv("ASK_FABLE_SAVE", "0")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def _run(coro):
    return asyncio.run(coro)


def _sidecar_text(recommendation, needs=None, prose="Here is the answer."):
    block = json.dumps({"sidecar_version": 1, "recommendation": recommendation,
                        "confidence": "medium", "needs_context": needs or []})
    return f"{prose}\n\n```json-sidecar\n{block}\n```"


def _stub_fable(monkeypatch, texts):
    """texts: a single string (repeated) or a list popped per call."""
    seq = texts if isinstance(texts, list) else None

    async def fake_run(question, context="", **kw):
        t = seq.pop(0) if seq else texts
        return OracleResult("ok", text=t, session_id="sid")

    monkeypatch.setattr(server.fable, "run", fake_run)


def test_followup_when_needs_context(monkeypatch):
    _stub_fable(monkeypatch, _sidecar_text("investigate", ["auth.py token check"]))
    out = _run(server._handle_ask(server.SessionStore(), {"question": "q", "context": "unrelated"}))
    assert out["status"] == "ok"
    assert out["followup"]["needs_context"] == ["auth.py token check"]
    assert "how" in out["followup"] and "session 'default'" in out["followup"]["how"]


def test_followup_flags_likely_already_pasted(monkeypatch):
    _stub_fable(monkeypatch, _sidecar_text("investigate", ["the handle function"]))
    out = _run(server._handle_ask(server.SessionStore(),
                                  {"question": "q", "context": "def handle(): pass"}))
    # 'handle' appears in the pasted context → flag it for re-reading, not re-pasting
    assert out["followup"]["likely_already_pasted"] == ["the handle function"]


def test_likely_present_uses_word_boundaries_not_substrings(monkeypatch):
    # "test"/"parser" must NOT match "latest"/"release" — a false "already pasted"
    # would make the agent withhold context the model actually needs.
    _stub_fable(monkeypatch, _sidecar_text("needs_more_context", ["the parser test suite"]))
    out = _run(server._handle_ask(server.SessionStore(),
                                  {"question": "q", "context": "see the latest release notes"}))
    assert "likely_already_pasted" not in out["followup"]  # no spurious substring hit


def test_no_followup_when_needs_context_empty(monkeypatch):
    _stub_fable(monkeypatch, _sidecar_text("apply", []))
    out = _run(server._handle_ask(server.SessionStore(), {"question": "q", "context": "x"}))
    assert out["status"] == "ok" and "followup" not in out


def test_context_exhausted_after_cap(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_MAX_NEEDS_CONTEXT", "1")  # exhaust on the 2nd consecutive
    _stub_fable(monkeypatch, _sidecar_text("needs_more_context", ["more code"]))
    store = server.SessionStore()
    o1 = _run(server._handle_ask(store, {"question": "q", "context": "x", "session": "s"}))
    assert o1["status"] == "ok"  # streak 1, cap 1 → not yet
    o2 = _run(server._handle_ask(store, {"question": "q", "context": "x", "session": "s"}))
    assert o2["status"] == "context_exhausted"  # streak 2 > 1
    assert "detail" in o2 and "followup" not in o2
    assert o2["answer"]  # best-effort answer still returned


def test_streak_resets_on_a_good_answer(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_MAX_NEEDS_CONTEXT", "1")
    _stub_fable(monkeypatch, [
        _sidecar_text("needs_more_context", ["x"]),  # streak 1
        _sidecar_text("apply", []),                   # reset to 0
        _sidecar_text("needs_more_context", ["x"]),  # streak 1 again, not exhausted
    ])
    store = server.SessionStore()
    outs = [_run(server._handle_ask(store, {"question": "q", "context": "x", "session": "s"}))
            for _ in range(3)]
    assert [o["status"] for o in outs] == ["ok", "ok", "ok"]  # reset prevents false exhaustion


def test_single_model_gets_followup(monkeypatch):
    async def fake_run(question, context="", **kw):
        return OracleResult("ok", text=_sidecar_text("investigate", ["config.yaml"]), model="MiniMax-M3")

    monkeypatch.setattr(server.minimax, "run", fake_run)
    out = _run(server._handle_m3({"question": "q", "context": "code"}))
    assert out["followup"]["needs_context"] == ["config.yaml"]
    assert "session" not in out["followup"]["how"]  # single-turn tools have no session
