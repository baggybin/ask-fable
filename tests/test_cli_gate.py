"""cli_gate — the opt-in bounded output reader (`max_output_bytes`), used by the run: sandbox.

The default path (`max_output_bytes=None`) is unchanged and exercised by every other bridge;
these tests pin the capped path: bounded retained output, exit-code fidelity, and timeout kill,
plus the spawn's never-raises contract for arguments no process can receive.
They spawn a real ``python3`` subprocess (fast).
"""

from __future__ import annotations

import sys

import pytest

from ask_fable import cli_gate


def _py(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


def test_capped_stdout_is_bounded():
    r = cli_gate.run_cli(_py("import sys; sys.stdout.write('x'*100000)"),
                         gate="test-cap", timeout=15, max_output_bytes=500)
    assert r.returncode == 0 and not r.timed_out
    assert len(r.stdout) == 500  # retained output capped, child still finished cleanly


def test_capped_stderr_is_bounded():
    r = cli_gate.run_cli(_py("import sys; sys.stderr.write('e'*100000)"),
                         gate="test-cap", timeout=15, max_output_bytes=300)
    assert r.returncode == 0 and len(r.stderr) == 300


def test_uncapped_returns_full_output():
    r = cli_gate.run_cli(_py("import sys; sys.stdout.write('y'*20000)"),
                         gate="test-cap", timeout=15)
    assert r.returncode == 0 and len(r.stdout) == 20000  # default path unchanged


def test_capped_preserves_exit_code():
    r = cli_gate.run_cli(_py("raise SystemExit(3)"),
                         gate="test-cap", timeout=15, max_output_bytes=100)
    assert r.returncode == 3 and not r.timed_out


def test_capped_timeout_kills_and_reaps():
    r = cli_gate.run_cli(_py("import time; time.sleep(30)"),
                         gate="test-cap", timeout=1, max_output_bytes=100)
    assert r.timed_out and r.returncode is None


@pytest.mark.parametrize("bad", ["find -print0 output: a\x00b", "lone \ud800 surrogate"])
def test_unpassable_argv_is_a_failed_run_not_an_exception(bad):
    """grok/gemini/codex/kimi put the prompt in argv. A NUL byte (ValueError) or an
    unencodable surrogate (UnicodeEncodeError) made Popen raise straight past the
    bridge's "never raises" contract — no audit row, no breaker record."""
    r = cli_gate.run_cli(_py("print('never spawned')") + [bad], gate="test-cap", timeout=15)
    assert r.returncode is None and not r.timed_out and r.stdout == ""
    assert "cannot pass the arguments" in r.stderr and "NUL" in r.stderr


def test_argv_rejection_is_classified_as_bad_input_not_backend_health():
    # A NUL byte in the prompt can't reach any process; the bridges route the failed
    # start through cli_error_detail, which must not charge it to the backend's breaker.
    from ask_fable import health
    from ask_fable.oracle_common import cli_error_detail

    run = cli_gate.run_cli(_py("print('never spawned')") + ["a\x00b"], gate="test-cap", timeout=15)
    kind, detail = cli_error_detail(
        label="grok", returncode=run.returncode, stderr=run.stderr, stdout=run.stdout
    )
    assert kind == "bad_input" and "NUL" in detail
    assert "bad_input" in health._NON_HEALTH_KINDS
