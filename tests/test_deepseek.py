"""ask_fable ask_deepseek handler — the standalone deepseek-v4-pro tool.

Drives ``_handle_deepseek`` with guard, the anthropic_http bridge, and audit
stubbed, and ASK_FABLE_QUIET=1 so the console reporter stays silent. No network
is hit.
"""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.anthropic_http as anthropic_http
import ask_fable.server as server
from ask_fable.anthropic_http import ProviderConfig
from ask_fable.oracle_common import OracleResult

# _handle_deepseek routes through oracles.run(); patch the anthropic_http bridge
# at its canonical module (the same object oracles imports), not via server.


@pytest.fixture(autouse=True)
def _quiet_and_no_audit(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


def _run(coro):
    return asyncio.run(coro)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def _cfg():
    return ProviderConfig(key="deepseek", label="deepseek-v4-pro", base_url="https://x",
                          model="deepseek-v4-pro", api_key="k")


def _configured(monkeypatch):
    monkeypatch.setattr(anthropic_http, "config_for", lambda key: _cfg() if key == "deepseek" else None)


def _stub(monkeypatch, result):
    async def fake_run(cfg, question, context="", **kw):
        fake_run.calls.append({"question": question, "context": context, "model": cfg.model})
        return result

    fake_run.calls = []
    monkeypatch.setattr(anthropic_http, "run", fake_run)
    return fake_run


def test_guard_denied_never_calls_model(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (False, "prohibited_x"))
    spy = _stub(monkeypatch, OracleResult("ok", text="should not happen"))
    out = _run(server._handle_deepseek({"question": "some blocked question here"}))
    assert out == {"status": "refused", "stage": "guard", "reason": "prohibited_x"}
    assert spy.calls == []


def test_not_configured_without_key(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(anthropic_http, "config_for", lambda key: None)
    spy = _stub(monkeypatch, OracleResult("ok", text="nope"))
    out = _run(server._handle_deepseek({"question": "How does dispatch work here?"}))
    assert out["status"] == "error" and out["kind"] == "not_configured"
    assert spy.calls == []  # never reaches the bridge


def test_ok_returns_answer_and_model(monkeypatch):
    _allow(monkeypatch)
    _configured(monkeypatch)
    spy = _stub(monkeypatch, OracleResult("ok", text="DeepSeek says hi", model="deepseek-v4-pro"))
    out = _run(server._handle_deepseek({"question": "How does routing work here?", "context": "def r(): ..."}))
    assert out == {"status": "ok", "model": "deepseek-v4-pro", "answer": "DeepSeek says hi",
                   "sidecar": None, "missing_sidecar": True}
    assert spy.calls[0]["context"] == "def r(): ..."


def test_model_refused(monkeypatch):
    _allow(monkeypatch)
    _configured(monkeypatch)
    _stub(monkeypatch, OracleResult("refused", text="not about code", model="deepseek-v4-pro"))
    out = _run(server._handle_deepseek({"question": "what is the best editor to use"}))
    assert out == {"status": "refused", "stage": "model", "reason": "not about code"}


def test_model_error(monkeypatch):
    _allow(monkeypatch)
    _configured(monkeypatch)
    _stub(monkeypatch, OracleResult("error", kind="timeout", text="slow", model="deepseek-v4-pro"))
    out = _run(server._handle_deepseek({"question": "Trace dispatch in this module."}))
    assert out["status"] == "error" and out["kind"] == "timeout" and out["detail"] == "slow"
