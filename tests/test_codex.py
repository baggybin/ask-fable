"""ask_fable Codex bridge — `codex exec` argv, output-file capture, timeout kill.

The ``codex`` CLI is mocked via a fake ``subprocess.Popen``; no real model is
called. The bridge reads the agent's final message from the ``--output-last-message``
file, so the fake writes that file as a side effect of being spawned.
"""

from __future__ import annotations

import asyncio
import subprocess

import ask_fable.codex as codex
from ask_fable import cli_gate, oracle_common


def _run(coro):
    return asyncio.run(coro)


from conftest import FakePopen as _FakePopen


def _patch(monkeypatch, seen=None, *, out_file="", **cfg):
    """Point cli_gate.subprocess.Popen at a _FakePopen, recording argv/kwargs. The
    fake writes ``out_file`` to the ``-o`` path the bridge passes, mimicking codex
    writing its final message there."""
    monkeypatch.setattr(codex.shutil, "which", lambda b: "/usr/bin/codex")
    fake = _FakePopen(**cfg)

    def _factory(argv, **kwargs):
        if seen is not None:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
        if out_file is not None:
            path = argv[argv.index("-o") + 1]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(out_file)
        return fake

    monkeypatch.setattr(cli_gate.subprocess, "Popen", _factory)
    return fake


def test_timeout_default_prefers_codex_var(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_TIMEOUT", "600")
    monkeypatch.delenv("ASK_FABLE_CODEX_TIMEOUT", raising=False)
    assert oracle_common.codex_timeout_default() == 600.0
    monkeypatch.setenv("ASK_FABLE_CODEX_TIMEOUT", "90")
    assert oracle_common.codex_timeout_default() == 90.0


def test_run_ok_and_argv(monkeypatch):
    seen = {}
    _patch(monkeypatch, seen, out_file="Handlers register in build_server().\n")
    res = _run(codex.run("How are handlers registered?", "def build(): ...", timeout=120))
    assert res.status == "ok" and res.text == "Handlers register in build_server()."
    assert res.model == "gpt-5.6-sol"
    argv = seen["argv"]
    assert argv[:2] == ["/usr/bin/codex", "exec"]
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    # hermetic + read-only, prompt is the last positional argument
    assert "--ignore-user-config" in argv and "--skip-git-repo-check" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=high"
    prompt = argv[-1]
    assert "QUESTION:" in prompt and "def build(): ..." in prompt
    # launched in its own session so we can group-kill the whole tree
    assert seen["kwargs"].get("start_new_session") is True
    # stdin is /dev/null — otherwise codex blocks reading the inherited (MCP) stdin pipe
    assert seen["kwargs"].get("stdin") == subprocess.DEVNULL
    assert "--json" in argv


def test_run_parses_jsonl_telemetry_but_file_remains_answer_authority(monkeypatch):
    stdout = "\n".join(
        (
            '{"type":"thread.started","thread_id":"thread-1"}',
            '{"type":"item.completed","item":{"type":"reasoning","text":"considered routes"}}',
            '{"type":"item.completed","item":{"type":"command_execution","id":"call-1","status":"completed","exit_code":0}}',
            '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":5}}',
            '{"type":"future.event","secret":"ignored"}',
        )
    )
    _patch(monkeypatch, out_file="authoritative answer", stdout=stdout)

    res = _run(codex.run("How does dispatch work?", timeout=120))

    assert res.text == "authoritative answer"
    assert res.thinking == "considered routes"
    assert res.telemetry is not None
    assert res.telemetry.provider_session_id == "thread-1"
    assert res.telemetry.usage is not None and res.telemetry.usage.input_tokens == 12
    assert res.telemetry.tool_events[0].name == "command_execution"
    assert res.telemetry.unknown_event_count == 1


def test_run_does_not_use_jsonl_stdout_as_answer_when_file_empty(monkeypatch):
    _patch(monkeypatch, out_file="", stdout="answer via stdout")
    res = _run(codex.run("How does dispatch work here?", timeout=120))
    assert res.status == "error" and res.kind == "sdk_error"


def test_jsonl_parser_fingerprints_non_object_events_and_scalar_items():
    stdout = "\n".join(("42", "[]", '{"type":"item.completed","item":7}'))

    thinking, telemetry = codex._parse_jsonl(stdout, "gpt-test", 1.0)

    assert thinking == ""
    assert telemetry.unknown_event_count == 3


def test_jsonl_parser_ignores_invalid_usage_token_counts():
    stdout = '{"type":"turn.completed","usage":{"input_tokens":true,"output_tokens":-2}}'

    _, telemetry = codex._parse_jsonl(stdout, "gpt-test", 1.0)

    assert telemetry.usage is None
    assert telemetry.usage_available is False


def test_run_refused(monkeypatch):
    _patch(monkeypatch, out_file="REFUSED: not about code")
    res = _run(codex.run("what is the best language overall really"))
    assert res.status == "refused" and res.text == "not about code"


def test_run_model_and_reasoning_override(monkeypatch):
    seen = {}
    monkeypatch.setenv("ASK_FABLE_CODEX_MODEL", "gpt-5.6")
    monkeypatch.setenv("ASK_FABLE_CODEX_REASONING", "xhigh")
    _patch(monkeypatch, seen, out_file="ok")
    res = _run(codex.run("How does dispatch work here?", timeout=120))
    assert res.model == "gpt-5.6"
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "gpt-5.6"
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=xhigh"


def test_run_empty_output_is_error(monkeypatch):
    _patch(monkeypatch, out_file="", stdout="   ", stderr="nothing came back")
    res = _run(codex.run("Trace dispatch in this module.", timeout=120))
    assert res.status == "error" and res.kind == "sdk_error" and res.text == "Codex returned no answer"


def test_run_nonzero(monkeypatch):
    _patch(monkeypatch, out_file="", returncode=1, stdout="", stderr="kaboom")
    res = _run(codex.run("Where is the router defined here?", timeout=120))
    assert res.status == "error" and res.kind == "sdk_error" and "kaboom" in res.text
    assert res.telemetry is not None and res.telemetry.tools_available is True
    assert res.telemetry.tool_events == ()


def test_run_timeout_kills_process_group(monkeypatch):
    killed = []
    monkeypatch.setattr(cli_gate.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli_gate.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    fake = _patch(monkeypatch, out_file="", timeout_first=True)
    res = _run(codex.run("Trace dispatch in this module.", timeout=1))
    assert res.status == "error" and res.kind == "timeout"
    assert res.telemetry is not None and res.telemetry.tools_available is True
    # the whole group (pgid == fake.pid via our getpgid stub) was SIGKILLed
    assert killed == [(fake.pid, cli_gate.signal.SIGKILL)]


def test_run_binary_missing(monkeypatch):
    spawned = {"popen": False}
    monkeypatch.setattr(codex.shutil, "which", lambda b: None)
    monkeypatch.setattr(
        cli_gate.subprocess, "Popen", lambda *a, **k: spawned.__setitem__("popen", True)
    )
    res = _run(codex.run("How does dispatch work in this file?"))
    assert res.status == "error" and res.kind == "binary_missing"
    assert spawned["popen"] is False  # never spawned
