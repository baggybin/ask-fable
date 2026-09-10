"""Bug hunt 2026-09-09 — cli_gate lifecycle (L1 cancel-kill, L2 mid-run exception)."""

from __future__ import annotations

import ask_fable.cli_gate as cli_gate
from ask_fable.cli_gate import _ProcHandle, run_cli


def test_non_timeout_exception_kills_child_and_reports(monkeypatch):
    """L2: a mid-run failure that is NOT TimeoutExpired must SIGKILL the child group
    and return a failed CliRun — never raise past the bridge contract, never orphan."""

    class BoomPopen:
        pid = 4242

        def __init__(self):
            self.killed = False

        def communicate(self, input=None, timeout=None):
            raise ValueError("pipe exploded")

        def kill(self):
            self.killed = True

    made = {}

    def factory(argv, **kw):
        made["p"] = BoomPopen()
        return made["p"]

    monkeypatch.setenv("ASK_FABLE_CLI_MAX_PARALLEL", "0")  # disable the gate
    monkeypatch.setattr(cli_gate.subprocess, "Popen", factory)
    monkeypatch.setattr(cli_gate, "_kill_group", lambda p: p.kill())
    cap = run_cli(["mycli", "-x"], gate="mycli", timeout=5)
    assert cap.returncode is None
    assert "failed mid-run" in cap.stderr and "ValueError" in cap.stderr
    assert made["p"].killed is True  # not orphaned


def test_proc_handle_kills_recorded_child(monkeypatch):
    """L1: a child spawned before cancel is killed when the async caller cancels."""
    killed = []
    monkeypatch.setattr(cli_gate, "_kill_group", killed.append)
    h = _ProcHandle()
    proc = object()
    assert h.set(proc) is True
    h.kill()
    assert killed == [proc]


def test_proc_handle_kills_child_spawned_after_cancel(monkeypatch):
    """L1: if cancel lands before the child is recorded, set() kills it immediately
    and tells the worker not to run it (returns False)."""
    killed = []
    monkeypatch.setattr(cli_gate, "_kill_group", killed.append)
    h = _ProcHandle()
    h.kill()  # cancelled before spawn
    proc = object()
    assert h.set(proc) is False
    assert killed == [proc]
