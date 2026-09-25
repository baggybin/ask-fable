"""sandbox — bubblewrap-isolated execution for ask_falsify run: receipts.

Default-off is checked without bwrap; the real-isolation tests require bwrap (skipped
otherwise) and set ASK_FABLE_ALLOW_RUN so the opt-in gate opens.
"""

from __future__ import annotations

import asyncio

import pytest

from ask_fable import cli_gate, sandbox


def _run(coro):
    return asyncio.run(coro)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_ALLOW_RUN", raising=False)
    r = _run(sandbox.run_python("print('x')"))
    assert r.status == "disabled" and r.returncode is None


def test_empty_snippet_is_error(monkeypatch):
    # Input is validated before the bwrap probe, so this holds on hosts without bwrap too.
    monkeypatch.setenv("ASK_FABLE_ALLOW_RUN", "1")
    monkeypatch.setattr(sandbox, "available", lambda: False)
    assert _run(sandbox.run_python("   ")).status == "error"


def test_argv_isolates():
    argv = sandbox._argv("/tmp/x.py", 512, 8)
    assert "--unshare-all" in argv and "--clearenv" in argv    # no net, no inherited env
    assert "--disable-userns" in argv                          # no nested userns re-entry
    assert "/home" not in argv and "/root" not in argv         # operator files never bound
    assert "/tmp/_run.py" in argv and "-I" in argv and "-u" in argv  # bound ro, isolated+unbuffered
    assert "--size" in argv                                     # tmpfs is size-capped


def test_no_host_wide_nproc_limit(monkeypatch):
    # prlimit --nproc is RLIMIT_NPROC: every task the user owns host-wide, threads
    # included. On a desktop past 256 of them bwrap's own clone failed (EAGAIN), so the
    # self-test failed and run: receipts never worked. The OUTER prlimit — the one that
    # runs as the host user, before bwrap — must therefore carry no --nproc.
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sandbox, "_systemd_scope", lambda: False)
    argv = sandbox._argv("/tmp/x.py", 512, 8)
    assert argv[0] == "/usr/bin/prlimit"  # the prlimit belt itself stays
    bwrap_at = next(i for i, a in enumerate(argv) if a.endswith("bwrap"))
    assert not any(a.startswith("--nproc") for a in argv[:bwrap_at])


def test_m5_a_fork_cap_applies_inside_the_user_namespace(monkeypatch, tmp_path):
    """M5 (bug hunt 2026-09-25): dropping the host-wide --nproc left NOTHING bounding
    forks when systemd-run is unavailable (no binary, or no XDG_RUNTIME_DIR — a
    headless host): --as is per-process and the pid namespace only reaps. RLIMIT_NPROC
    is counted per USER NAMESPACE since Linux 5.14, so the cap goes inside bwrap."""
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sandbox.os.path, "exists", lambda p: p == "/usr/bin/prlimit")
    monkeypatch.setattr(sandbox, "_systemd_scope", lambda: False)
    argv = sandbox._argv("/tmp/x.py", 512, 8)
    py = argv.index("python3")
    inner = argv[:py]
    assert inner[-3:] == ["/usr/bin/prlimit", f"--nproc={sandbox._INNER_NPROC}", "--"]
    # and it sits AFTER --unshare-user, or it would be the host-wide limit again
    assert argv.index("--unshare-user") < py - 3


def _fake_run(monkeypatch, returncode, stderr):
    async def run_cli_async(argv, **kw):
        return cli_gate.CliRun(returncode, "", stderr, timed_out=False)

    monkeypatch.setenv("ASK_FABLE_ALLOW_RUN", "1")
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "_systemd_scope", lambda: False)
    monkeypatch.setattr(sandbox.cli_gate, "run_cli_async", run_cli_async)


@pytest.mark.parametrize("returncode, stderr", [
    (1, "bwrap: Creating new namespace failed: Resource temporarily unavailable\n"),
    (1, "bwrap: execvp python3: No such file or directory\n"),
    (1, "prlimit: failed to set the AS resource limit: Operation not permitted\n"),
    (1, "Traceback (most recent call last):\n"
        "BlockingIOError: [Errno 11] Resource temporarily unavailable\n"),
    (None, "failed to start systemd-run: [Errno 11] Resource temporarily unavailable"),
])
def test_sandbox_failure_is_not_a_failing_run(monkeypatch, returncode, stderr):
    # falsify reads a "fail" as the snippet disproving a claim; a sandbox that could not
    # set up or ran out of processes must come back as a non-verdict instead.
    _fake_run(monkeypatch, returncode, stderr)
    assert _run(sandbox.run_python("print(1)")).status == "unavailable"


def test_snippet_failure_is_still_a_failing_run(monkeypatch):
    _fake_run(monkeypatch, 1, "Traceback (most recent call last):\nAssertionError\n")
    r = _run(sandbox.run_python("assert False"))
    assert r.status == "fail" and r.returncode == 1


_needs_bwrap = pytest.mark.skipif(not sandbox.available(), reason="bwrap not installed")


@_needs_bwrap
def test_runs_and_captures_stdout(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALLOW_RUN", "1")
    r = _run(sandbox.run_python("print('hello-sbx')"))
    assert r.status == "ok" and r.returncode == 0 and "hello-sbx" in r.stdout


@_needs_bwrap
def test_nonzero_exit_is_fail(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALLOW_RUN", "1")
    r = _run(sandbox.run_python("raise SystemExit(3)"))
    assert r.status == "fail" and r.returncode == 3


@_needs_bwrap
def test_network_and_filesystem_isolated(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALLOW_RUN", "1")
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=1); print('NET-OPEN')\n"
        "except Exception: print('NET-BLOCKED')\n"
        "try:\n"
        "    open('/etc/hostname'); print('FS-OPEN')\n"
        "except Exception: print('FS-ISOLATED')\n"
    )
    r = _run(sandbox.run_python(code))
    assert r.status == "ok"
    assert "NET-BLOCKED" in r.stdout and "NET-OPEN" not in r.stdout
    assert "FS-ISOLATED" in r.stdout and "FS-OPEN" not in r.stdout


@_needs_bwrap
def test_timeout_kills_runaway(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALLOW_RUN", "1")
    monkeypatch.setenv("ASK_FABLE_RUN_TIMEOUT_S", "2")
    assert _run(sandbox.run_python("while True:\n    pass\n")).status == "timeout"


@_needs_bwrap
def test_self_test_passes_when_enabled(monkeypatch):
    # gates run: receipts — the exit-0 and exit-3 cases must round-trip through the real path
    monkeypatch.setenv("ASK_FABLE_ALLOW_RUN", "1")
    assert _run(sandbox.self_test()) is True


def test_self_test_false_when_disabled(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_ALLOW_RUN", raising=False)
    assert _run(sandbox.self_test()) is False
