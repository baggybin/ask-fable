"""ask_fable Fable invocation — response shaping, CLI bridge, dispatch.

The SDK path is exercised via `run()` dispatch (with `_run_sdk` stubbed); the CLI
bridge is tested by mocking `subprocess.Popen`. No real model is ever called.
"""

from __future__ import annotations

import asyncio

import ask_fable.fable as fable
from ask_fable import cli_gate, oracle_common
from ask_fable.oracle_common import OracleResult


def _run(coro):
    return asyncio.run(coro)


def test_shape_ok_refused_empty():
    assert oracle_common.shape("The router dispatches to _handle.").status == "ok"
    r = oracle_common.shape("REFUSED: question is too broad")
    assert r.status == "refused" and r.text == "question is too broad"
    assert oracle_common.shape("REFUSED:").status == "refused"  # empty reason -> default
    assert oracle_common.shape("   ").status == "error" and oracle_common.shape("").kind == "sdk_error"


from conftest import FakePopen as _FakePopen


def _stub_gate(monkeypatch, seen: dict):
    """Record cli_gate acquisition without touching the real semaphores."""
    import contextlib

    @contextlib.contextmanager
    def fake_hold(name, timeout=None):
        seen["gate"] = name
        yield

    monkeypatch.setattr(cli_gate, "hold", fake_hold)


def test_cli_ok_and_argv(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        cli_gate.subprocess, "Popen",
        _FakePopen.factory(stdout="Handlers are registered in build_server()."),
    )
    _stub_gate(monkeypatch, seen)
    res = _run(fable.run("How are handlers registered?", "def build(): ...", use_cli=True))
    assert res.status == "ok" and "Handlers" in res.text
    proc = _FakePopen.instances.pop()
    argv = proc.argv
    assert "-p" in argv and fable.fable_model() in argv
    assert "--strict-mcp-config" in argv
    # tools disabled: the flag is present with an empty-string value
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    # question travels on stdin, not argv
    assert proc.stdin_data.startswith("QUESTION:")
    assert not any("QUESTION" in a for a in argv)
    # hardened spawn: own session so the group can be killed, and gated per-binary
    assert proc.kw.get("start_new_session") is True
    assert seen["gate"] == "claude"


def test_cli_refused(monkeypatch):
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(cli_gate.subprocess, "Popen", _FakePopen.factory(stdout="REFUSED: not about code"))
    res = _run(fable.run("what is the best language overall really", use_cli=True))
    assert res.status == "refused" and res.text == "not about code"


def test_cli_non_string_result_is_structured_error(monkeypatch):
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        cli_gate.subprocess, "Popen", _FakePopen.factory(stdout='{"result":{"nested":true}}')
    )

    res = _run(fable.run("How does dispatch work?", use_cli=True))

    assert res.status == "error" and res.kind == "sdk_error"


def test_cli_timeout_kills_process_group(monkeypatch):
    killed: dict = {}
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(cli_gate.subprocess, "Popen", _FakePopen.factory(timeout_first=True))
    monkeypatch.setattr(cli_gate.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli_gate.os, "killpg", lambda pgid, sig: killed.__setitem__("pgid", pgid))
    res = _run(fable.run("Does the parser handle nested fences here?", use_cli=True, timeout=1))
    assert res.status == "error" and res.kind == "timeout"
    assert "pgid" in killed  # the whole process group was SIGKILLed, not just the child


def test_cli_nonzero_surfaces_stderr(monkeypatch):
    """A failed claude CLI routes through cli_error_detail like every other CLI
    bridge — stderr is surfaced, not swallowed behind a constant string."""
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(cli_gate.subprocess, "Popen", _FakePopen.factory(returncode=1, stderr="kaboom"))
    res = _run(fable.run("Where is the router defined in this module?", use_cli=True))
    assert res.status == "error" and res.kind == "sdk_error"
    assert "kaboom" in res.text


def test_cli_usage_limit_is_rate_limit(monkeypatch):
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        cli_gate.subprocess,
        "Popen",
        _FakePopen.factory(returncode=1, stderr="usage limit reached — resets at 5pm"),
    )
    res = _run(fable.run("Where is the router defined in this module?", use_cli=True))
    assert res.status == "error" and res.kind == "rate_limit"
    assert "usage limit" in res.text


def test_cli_logged_out_is_auth_failed(monkeypatch):
    """An expired login must classify auth_failed (breaker-exempt) so the
    actionable message keeps surfacing instead of circuit_open."""
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        cli_gate.subprocess,
        "Popen",
        _FakePopen.factory(returncode=1, stderr="Not logged in. Please run /login"),
    )
    res = _run(fable.run("Where is the router defined in this module?", use_cli=True))
    assert res.status == "error" and res.kind == "auth_failed"
    assert "/login" in res.text


def test_cli_binary_missing(monkeypatch):
    called = {"popen": False}

    def mark(*a, **k):
        called["popen"] = True
        raise AssertionError("should not spawn")

    monkeypatch.setattr(fable.shutil, "which", lambda b: None)
    monkeypatch.setattr(cli_gate.subprocess, "Popen", mark)
    res = _run(fable.run("How does dispatch work in this file?", use_cli=True))
    assert res.status == "error" and res.kind == "binary_missing"
    assert called["popen"] is False  # never spawned


def test_run_dispatches_to_sdk_with_resume(monkeypatch):
    seen = {}

    async def fake_sdk(message, timeout, resume, system_prompt=None, on_think=None, spec=None):
        seen["resume"] = resume
        seen["message"] = message
        seen["system_prompt"] = system_prompt
        seen["spec"] = spec
        return OracleResult("ok", text="ok", session_id="sid-2")

    monkeypatch.setattr(fable, "_run_sdk", fake_sdk)
    res = _run(fable.run("Trace the call path from run() to _handle.", resume="sid-1"))
    assert res.status == "ok" and res.session_id == "sid-2"
    assert seen["resume"] == "sid-1" and seen["message"].startswith("QUESTION:")
    # the model spec defaults to Fable, resolved through the ladder
    assert seen["spec"].key == "fable" and seen["spec"].model == fable.fable_model()


def test_run_falls_back_to_cli_on_sdk_importerror(monkeypatch):
    async def boom(*a, **k):
        raise ImportError("no sdk")

    async def fake_cli(message, timeout, system_prompt=None, spec=None):
        return OracleResult("ok", text="from-cli")

    monkeypatch.setattr(fable, "_run_sdk", boom)
    monkeypatch.setattr(fable, "_run_cli", fake_cli)
    res = _run(fable.run("How does the module route requests internally?"))
    assert res.status == "ok" and res.text == "from-cli"
