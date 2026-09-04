"""ask_fable ask_m3 handler — the standalone MiniMax-M3 tool.

Drives ``_handle_m3`` with guard, minimax, and audit stubbed, and
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


def _stub(monkeypatch, result):
    async def fake_run(question, context="", **kw):
        fake_run.calls.append({"question": question, "context": context})
        return result

    fake_run.calls = []
    monkeypatch.setattr(server.minimax, "run", fake_run)
    return fake_run


def test_guard_denied_never_calls_model(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (False, "prohibited_x"))
    spy = _stub(monkeypatch, OracleResult("ok", text="should not happen"))
    out = _run(server._handle_m3({"question": "some blocked question here"}))
    assert out == {"status": "refused", "stage": "guard", "reason": "prohibited_x"}
    assert spy.calls == []


def test_ok_returns_answer_and_model(monkeypatch):
    _allow(monkeypatch)
    spy = _stub(monkeypatch, OracleResult("ok", text="MiniMax says hi", model="MiniMax-M3"))
    out = _run(server._handle_m3({"question": "How does routing work here?", "context": "def r(): ..."}))
    assert out == {"status": "ok", "model": "MiniMax-M3", "answer": "MiniMax says hi",
                   "sidecar": None, "missing_sidecar": True}
    assert spy.calls[0]["context"] == "def r(): ..."


def test_ok_extracts_and_strips_sidecar(monkeypatch):
    _allow(monkeypatch)
    raw = (
        'Use lru_cache — it is pure and hashable.\n\n'
        '```json-sidecar\n{"sidecar_version": 1, "recommendation": "apply", '
        '"confidence": "high", "needs_context": []}\n```'
    )
    _stub(monkeypatch, OracleResult("ok", text=raw, model="MiniMax-M3"))
    out = _run(server._handle_m3({"question": "cache it?", "context": "def f(): ..."}))
    assert out["answer"] == "Use lru_cache — it is pure and hashable."  # block stripped
    assert out["missing_sidecar"] is False
    assert out["sidecar"] == {"recommendation": "apply", "confidence": "high", "needs_context": []}


def test_model_refused(monkeypatch):
    _allow(monkeypatch)
    _stub(monkeypatch, OracleResult("refused", text="not about code", model="MiniMax-M3"))
    out = _run(server._handle_m3({"question": "what is the best editor to use"}))
    assert out == {"status": "refused", "stage": "model", "reason": "not about code"}


def test_model_error(monkeypatch):
    _allow(monkeypatch)
    _stub(monkeypatch, OracleResult("error", kind="binary_missing", text="no mmx", model="MiniMax-M3"))
    out = _run(server._handle_m3({"question": "Trace dispatch in this module."}))
    assert out["status"] == "error" and out["kind"] == "binary_missing" and out["detail"] == "no mmx"
