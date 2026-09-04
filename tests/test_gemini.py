"""ask_fable Gemini bridge — `agy` CLI argv, plain-text output, timeout kill.

The ``agy`` CLI is mocked via a fake ``subprocess.Popen``; no real model is called.
"""

from __future__ import annotations

import asyncio
import subprocess

import ask_fable.gemini as gemini
from ask_fable import cli_gate, oracle_common


def _run(coro):
    return asyncio.run(coro)


from conftest import FakePopen as _FakePopen


def _patch(monkeypatch, seen=None, **cfg):
    """Point cli_gate.subprocess.Popen at a _FakePopen, recording argv/kwargs."""
    monkeypatch.setattr(gemini.shutil, "which", lambda b: "/usr/bin/agy")
    fake = _FakePopen(**cfg)

    def _factory(argv, **kwargs):
        if seen is not None:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(cli_gate.subprocess, "Popen", _factory)
    return fake


def test_timeout_default_prefers_gemini_var(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_TIMEOUT", "600")
    monkeypatch.delenv("ASK_FABLE_GEMINI_TIMEOUT", raising=False)
    assert oracle_common.gemini_timeout_default() == 600.0
    monkeypatch.setenv("ASK_FABLE_GEMINI_TIMEOUT", "90")
    assert oracle_common.gemini_timeout_default() == 90.0


def test_run_ok_and_argv(monkeypatch):
    seen = {}
    _patch(monkeypatch, seen, stdout="Handlers register in build_server().\n")
    res = _run(gemini.run("How are handlers registered?", "def build(): ...", timeout=120))
    assert res.status == "ok" and res.text == "Handlers register in build_server()."
    assert res.model == "Gemini 3.1 Pro (High)"
    argv = seen["argv"]
    assert argv[1:3] == ["--model", "Gemini 3.1 Pro (High)"]
    # agy gets a soft self-limit just inside our hard timeout, and the prompt via -p
    assert "--print-timeout" in argv and argv[argv.index("--print-timeout") + 1] == "110s"
    assert argv[-2] == "-p"
    prompt = argv[-1]
    assert "QUESTION:" in prompt and "def build(): ..." in prompt
    # launched in its own session so we can group-kill the whole tree
    assert seen["kwargs"].get("start_new_session") is True
    # stdin is /dev/null — otherwise agy blocks reading the inherited (MCP) stdin pipe
    assert seen["kwargs"].get("stdin") == subprocess.DEVNULL


def test_gemini_timeout_env_shapes_print_timeout(monkeypatch):
    seen = {}
    monkeypatch.setenv("ASK_FABLE_TIMEOUT", "600")
    monkeypatch.setenv("ASK_FABLE_GEMINI_TIMEOUT", "90")
    _patch(monkeypatch, seen, stdout="ok")
    res = _run(gemini.run("How does dispatch work here?"))  # no explicit timeout -> env
    assert res.status == "ok"
    argv = seen["argv"]
    assert argv[argv.index("--print-timeout") + 1] == "80s"  # 90 - 10


def test_run_refused(monkeypatch):
    _patch(monkeypatch, stdout="REFUSED: not about code")
    res = _run(gemini.run("what is the best language overall really"))
    assert res.status == "refused" and res.text == "not about code"


def test_run_model_override(monkeypatch):
    seen = {}
    monkeypatch.setenv("ASK_FABLE_GEMINI_MODEL", "Gemini 3.5 Flash (Low)")
    _patch(monkeypatch, seen, stdout="ok")
    res = _run(gemini.run("How does dispatch work here?", timeout=120))
    assert res.model == "Gemini 3.5 Flash (Low)"
    assert seen["argv"][seen["argv"].index("--model") + 1] == "Gemini 3.5 Flash (Low)"


def test_run_empty_output_is_error(monkeypatch):
    _patch(monkeypatch, stdout="   ", stderr="nothing came back")
    res = _run(gemini.run("Trace dispatch in this module.", timeout=120))
    assert res.status == "error" and res.kind == "sdk_error" and res.text == "Gemini returned no answer"


def test_run_nonzero(monkeypatch):
    _patch(monkeypatch, returncode=1, stdout="", stderr="kaboom")
    res = _run(gemini.run("Where is the router defined here?", timeout=120))
    assert res.status == "error" and res.kind == "sdk_error" and "kaboom" in res.text
    assert res.telemetry is not None and res.telemetry.tools_available is None
    assert res.telemetry.reasoning_available is None


def test_run_timeout_kills_process_group(monkeypatch):
    killed = []
    monkeypatch.setattr(cli_gate.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli_gate.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    fake = _patch(monkeypatch, timeout_first=True)
    res = _run(gemini.run("Trace dispatch in this module.", timeout=1))
    assert res.status == "error" and res.kind == "timeout"
    assert res.telemetry is not None and res.telemetry.tools_available is None
    # the whole group (pgid == fake.pid via our getpgid stub) was SIGKILLed
    assert killed == [(fake.pid, cli_gate.signal.SIGKILL)]


def test_run_binary_missing(monkeypatch):
    spawned = {"popen": False}
    monkeypatch.setattr(gemini.shutil, "which", lambda b: None)
    monkeypatch.setattr(cli_gate.subprocess, "Popen", lambda *a, **k: spawned.__setitem__("popen", True))
    res = _run(gemini.run("How does dispatch work in this file?"))
    assert res.status == "error" and res.kind == "binary_missing"
    assert spawned["popen"] is False  # never spawned
