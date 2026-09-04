"""Claude Opus 5 as a first-class oracle.

Three layers: the bridge (same Claude transport as Fable, different model id),
the oracle registry (`opus` token usable anywhere `fable` is), and the
`ask_opus5` tool (the multi-turn twin of `ask`, with its own session namespace).
No model is ever called.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from conftest import FakePopen as _FakePopen

import ask_fable.fable as fable
import ask_fable.opus as opus
import ask_fable.oracles as oracles
import ask_fable.server as server
from ask_fable import cli_gate
from ask_fable.oracle_common import OracleResult
from ask_fable.sessions import SessionStore


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")


def _stub_gate(monkeypatch):
    @contextlib.contextmanager
    def fake_hold(name, timeout=None):
        yield

    monkeypatch.setattr(cli_gate, "hold", fake_hold)


# --- bridge ---------------------------------------------------------------


def test_cli_bridge_asks_for_the_opus_model(monkeypatch):
    """Same `claude -p` transport as Fable; only --model differs."""
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        cli_gate.subprocess, "Popen", _FakePopen.factory(stdout="Opus answered.")
    )
    _stub_gate(monkeypatch)
    res = _run(opus.run("How are handlers registered?", "def build(): ...", use_cli=True))
    assert res.status == "ok" and res.text == "Opus answered."
    argv = _FakePopen.instances.pop().argv
    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    assert fable.FABLE_MODEL not in argv
    # telemetry is attributed to opus, not to the module hosting the transport
    assert res.telemetry.oracle_key == "opus"
    assert res.telemetry.requested_model == "claude-opus-5"


def test_cli_failures_are_labeled_opus(monkeypatch):
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        cli_gate.subprocess, "Popen", _FakePopen.factory(timeout_first=True)
    )
    _stub_gate(monkeypatch)
    res = _run(opus.run("Where is the router defined?", use_cli=True, timeout=1))
    assert res.status == "error" and res.kind == "timeout"
    assert res.text.startswith("Opus 5 CLI timed out")  # not "Fable"


def test_sdk_path_carries_the_opus_spec(monkeypatch):
    seen: dict = {}

    async def fake_sdk(message, timeout, resume, system_prompt=None, on_think=None, spec=None):
        seen["spec"] = spec
        seen["resume"] = resume
        return OracleResult("ok", text="ok", session_id="sid-9")

    monkeypatch.setattr(fable, "_run_sdk", fake_sdk)
    res = _run(opus.run("Trace the call path.", resume="sid-8"))
    assert res.session_id == "sid-9" and seen["resume"] == "sid-8"
    assert seen["spec"] is opus.OPUS
    assert seen["spec"].model == "claude-opus-5" and seen["spec"].key == "opus"


# --- registry -------------------------------------------------------------


def test_registered_as_a_council_oracle():
    assert "opus" in oracles.KNOWN
    assert oracles.label("opus") == opus.OPUS_MODEL
    # rides Claude Code's OAuth session, like fable — no key or CLI to configure
    assert oracles.available("opus") is True
    # canonical order puts the two Anthropic models first, fable leading
    assert oracles.resolve(["minimax", "opus", "fable"]) == (["fable", "opus", "minimax"], [])
    # operator aliases
    assert oracles.resolve_ordered(["opus5", "OPUS-5", "claude-opus-5"]) == (
        ["opus", "opus", "opus"],
        [],
    )


def test_run_dispatches_to_the_opus_bridge(monkeypatch):
    seen: dict = {}

    async def fake_opus(q, c="", **kw):
        seen.update(kw)
        return OracleResult("ok", text="opus ans", thinking="th")

    monkeypatch.setattr(oracles.opus, "run", fake_opus)
    r = _run(oracles.run("opus", "q"))
    assert r.key == "opus" and r.text == "opus ans" and r.model == opus.OPUS_MODEL
    # the system-prompt channel is used directly (not folded into the message),
    # exactly as for fable — both are Claude Agent SDK bridges
    assert "system_prompt" in seen


def test_can_synthesize_a_council(monkeypatch):
    async def fake_opus(q, c="", **kw):
        assert kw.get("system_prompt")  # SYNTH prompt rides the system channel
        return OracleResult("ok", text="merged")

    monkeypatch.setattr(oracles.opus, "run", fake_opus)
    r = _run(oracles.run_synthesis("opus", "reconcile these"))
    assert r.status == "ok" and r.text == "merged" and r.key == "opus"


# --- ask_opus5 tool -------------------------------------------------------


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


def _stub(monkeypatch, module, result):
    async def fake_run(question, context="", *, resume=None):
        fake_run.calls.append({"question": question, "resume": resume})
        return result

    fake_run.calls = []
    monkeypatch.setattr(module, "run", fake_run)
    return fake_run


def test_tool_is_registered_with_the_ask_grammar():
    assert server._TOOL_SCHEMAS["ask_opus5"] is server._OPUS_SCHEMA
    assert server.ASK_OPUS5_TOOL_DESCRIPTION  # imported for list_tools registration
    # same argument grammar as `ask` — it's the same tool on a different model
    assert set(server._OPUS_SCHEMA["properties"]) == set(server._ASK_SCHEMA["properties"])
    assert server._schema_error("ask_opus5", {"question": "q", "session": "s"}) is None
    assert server._schema_error("ask_opus5", {"question": "q", "bogus": 1}) is not None


def test_answers_and_reports_the_opus_model(monkeypatch):
    _allow(monkeypatch)
    _stub(monkeypatch, server.opus, OracleResult("ok", text="The router dispatches."))
    out = _run(server._handle_opus5(SessionStore(), {"question": "How does routing work here?"}))
    assert out["status"] == "ok" and out["model"] == opus.OPUS_MODEL
    assert out["answer"] == "The router dispatches." and out["session"] == "default"


def test_sessions_are_namespaced_away_from_ask(monkeypatch):
    """An SDK session id belongs to the model that made it — the same key on both
    tools must be two conversations, never a cross-model resume."""
    _allow(monkeypatch)
    fable_spy = _stub(monkeypatch, server.fable, OracleResult("ok", text="f", session_id="f-1"))
    opus_spy = _stub(monkeypatch, server.opus, OracleResult("ok", text="o", session_id="o-1"))
    store = SessionStore()

    _run(server._handle_ask(store, {"question": "First routing question.", "session": "s"}))
    _run(server._handle_opus5(store, {"question": "First routing question.", "session": "s"}))
    # neither first turn resumed anything
    assert fable_spy.calls[0]["resume"] is None and opus_spy.calls[0]["resume"] is None

    _run(server._handle_ask(store, {"question": "And the error path?", "session": "s"}))
    _run(server._handle_opus5(store, {"question": "And the error path?", "session": "s"}))
    # each follow-up resumes its OWN thread
    assert fable_spy.calls[-1]["resume"] == "f-1"
    assert opus_spy.calls[-1]["resume"] == "o-1"


def test_reset_session_reaches_the_opus_namespace(monkeypatch, tmp_path):
    _allow(monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _stub(monkeypatch, server.opus, OracleResult("ok", text="answer one", session_id="o-1"))
    store = SessionStore()
    _run(server._handle_opus5(store, {"question": "First routing question.", "session": "s"}))
    # default model='fable' clears a DIFFERENT (empty) session, leaving opus intact
    assert server._handle_reset(store, {"session": "s"})["dump"] is None
    assert store.resume_id(server.OPUS_SESSION_NS + "s") == "o-1"
    # model='opus5' applies the same prefix the tool used
    out = server._handle_reset(store, {"session": "s", "model": "opus5"})
    assert out["session"] == "s" and out["dump"]
    assert store.resume_id(server.OPUS_SESSION_NS + "s") is None


def test_reset_session_accepts_opus_aliases():
    """An agent that learned 'opus' from the council docs must not silently clear
    the FABLE session instead of the Opus one."""
    enum = server._RESET_SCHEMA["properties"]["model"]["enum"]
    for tok in ("opus", "opus5", "opus-5", "claude-opus-5"):
        assert tok in enum, tok
    for tok in ("opus", "opus5", "opus-5", "claude-opus-5"):
        canon = oracles.ALIASES.get(tok, tok)
        assert canon == "opus", f"{tok} -> {canon}"
    assert oracles.ALIASES.get("fable", "fable") != "opus"
