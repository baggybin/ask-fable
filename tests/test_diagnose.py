"""The `diagnose` tool — read-only backend health.

Covers the hard contract (never records a breaker outcome, never calls the real
oracle path), the rollup rules (unconfigured-by-design is never an error;
on-PATH-but-broken is), and the breaker `snapshot` it reads. No model is called.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import ask_fable.diagnose as diagnose
import ask_fable.oracles as oracles
import ask_fable.server as server
from ask_fable.health import Breaker, _State, breaker


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")


@pytest.fixture(autouse=True)
def _clean_breaker():
    # Each test starts and ends with a clean global breaker.
    breaker._states.clear()
    yield
    breaker._states.clear()


# --- Breaker.snapshot (read-only) -----------------------------------------


def test_snapshot_is_read_only_and_shapes_a_gate():
    # A closed, unknown oracle.
    assert breaker.snapshot("nope") == {
        "state": "closed",
        "skip_reason": None,
        "resume_in_s": None,
        "held_until": 0.0,
        "hold_step": 0,
        "window": 0,
    }
    # Force an open breaker by hand; snapshot must report it without mutating.
    breaker._states["x"] = _State(opened_at=time.monotonic())
    before = dict(vars(breaker._states["x"]))
    snap = breaker.snapshot("x")
    assert snap["state"] == "open" and snap["skip_reason"] == "circuit_open"
    assert snap["resume_in_s"] is not None
    assert dict(vars(breaker._states["x"])) == before  # snapshot mutated nothing


def test_snapshot_reports_quota_hold_forward_compatibly():
    breaker._states["y"] = _State(held_until=time.monotonic() + 120)
    snap = breaker.snapshot("y")
    assert snap["state"] == "closed"  # a hold is not a health trip
    assert snap["skip_reason"] == "quota_hold" and snap["resume_in_s"] > 0


# --- probe / rollup rules --------------------------------------------------


def _patch_cli(monkeypatch, result):
    monkeypatch.setattr(diagnose, "_cli_version", lambda binary, **kw: result)


def test_reachable_cli_is_ok(monkeypatch):
    monkeypatch.setattr(oracles, "available", lambda k: True)
    _patch_cli(monkeypatch, (True, "mmx 1.2.3"))
    row = _run(diagnose._probe("minimax"))
    assert row["status"] == "ok" and row["checks"][0]["ok"] is True
    assert "fix" not in row


def test_cli_on_path_but_broken_is_error(monkeypatch):
    monkeypatch.setattr(oracles, "available", lambda k: True)
    _patch_cli(monkeypatch, (False, "`codex --version` exited 1"))
    row = _run(diagnose._probe("codex"))
    assert row["status"] == "error" and row["fix"]  # configured AND broken


def test_unconfigured_cli_is_not_configured_never_error(monkeypatch):
    monkeypatch.setattr(oracles, "available", lambda k: False)
    row = _run(diagnose._probe("grok"))
    assert row["status"] == "not_configured" and "install the `grok`" in row["fix"]


def test_unconfigured_http_backend(monkeypatch):
    monkeypatch.setattr(oracles, "available", lambda k: k != "deepseek")
    row = _run(diagnose._probe("deepseek"))
    assert row["status"] == "not_configured"
    assert "ASK_FABLE_DEEPSEEK_API_KEY" in row["fix"]


def test_oauth_backend_never_hits_the_api(monkeypatch):
    """The reachability signal is Claude Code's *presence* — no completion, no token
    refresh, no network. Presence is stubbed so the assertion holds on a host
    without Claude Code too (a bare `available()` used to make this vacuous)."""
    monkeypatch.setattr(diagnose.fable, "claude_code_present", lambda: True)
    row = _run(diagnose._probe("fable"))
    assert row["status"] == "ok"
    assert row["checks"][0]["name"] == "claude_code"  # a local probe, not a completion
    assert row["checks"][0]["detail"] == "Claude Agent SDK or `claude` CLI on PATH"


def test_open_breaker_downgrades_ok_to_warning(monkeypatch):
    monkeypatch.setattr(oracles, "available", lambda k: True)
    monkeypatch.setattr(
        diagnose.breaker,
        "snapshot",
        lambda k: {"state": "open", "skip_reason": "circuit_open", "resume_in_s": 42.0},
    )
    row = _run(diagnose._probe("fable"))
    assert row["status"] == "warning" and row["gate"]["skip_reason"] == "circuit_open"


def test_rollup_precedence():
    err = [{"status": "error"}, {"status": "ok"}, {"status": "not_configured"}]
    warn = [{"status": "warning"}, {"status": "ok"}, {"status": "not_configured"}]
    fine = [{"status": "ok"}, {"status": "not_configured"}]
    assert diagnose._rollup(err) == "error"
    assert diagnose._rollup(warn) == "warning"
    assert diagnose._rollup(fine) == "ok"  # not_configured alone never escalates


# --- the hard contract -----------------------------------------------------


def test_run_never_records_a_breaker_outcome(monkeypatch):
    # If diagnose ever fed the breaker, this raise would surface.
    def boom(*a, **k):
        raise AssertionError("diagnose must never call breaker.record")

    monkeypatch.setattr(Breaker, "record", boom)
    _patch_cli(monkeypatch, (True, "ok"))
    out = _run(diagnose.run())
    assert out["status"] == "ok" and out["rollup"] in ("ok", "warning", "error")


def test_run_never_calls_the_oracle_path(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("diagnose must never call oracles.run")

    monkeypatch.setattr(oracles, "run", boom)
    _patch_cli(monkeypatch, (True, "ok"))
    out = _run(diagnose.run())
    assert out["checked"] == len(oracles.KNOWN)


def test_run_leaves_breaker_state_unchanged(monkeypatch):
    _patch_cli(monkeypatch, (True, "ok"))
    breaker._states["fable"] = _State(opened_at=time.monotonic())
    before = {k: dict(vars(v)) for k, v in breaker._states.items()}
    _run(diagnose.run())
    after = {k: dict(vars(v)) for k, v in breaker._states.items()}
    assert before == after


def test_run_shape_and_stable_order(monkeypatch):
    _patch_cli(monkeypatch, (True, "ok"))
    out = _run(diagnose.run())
    assert out["schema_version"] == diagnose.SCHEMA_VERSION
    keys = [r["key"] for r in out["oracles"]]
    assert keys == sorted(keys)  # stable, sorted by key
    for r in out["oracles"]:
        assert set(r["gate"]) == {"state", "skip_reason", "resume_in_s"}
        assert "resolved_model" in r and "timeout_s" in r


def test_cli_version_not_on_path(monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda b: None)
    ok, detail = diagnose._cli_version("definitely-not-a-real-binary")
    assert ok is False and detail == "not on PATH"


def test_cli_version_survives_non_utf8_output(tmp_path):
    # `--version` output was decoded strictly, so one non-UTF-8 byte (here Latin-1 é)
    # raised UnicodeDecodeError out of the probe instead of reporting the version.
    cli = tmp_path / "fakecli"
    cli.write_bytes(b"#!/bin/sh\nprintf 'fakecli 1.0 \\351dition\\n'\n")
    cli.chmod(0o755)
    ok, detail = diagnose._cli_version(str(cli))
    assert ok is True and detail.startswith("fakecli 1.0 ")


def test_a_probe_that_raises_is_one_error_row_not_a_lost_report(monkeypatch):
    # gather() without return_exceptions let one probe's exception escape run(), so the
    # tool lost the whole report (sdk_error) instead of flagging the one broken probe.
    _patch_cli(monkeypatch, (True, "ok"))
    real_probe = diagnose._probe
    victim = sorted(oracles.KNOWN)[0]

    async def probe(key):
        if key == victim:
            raise RuntimeError("probe blew up")
        return await real_probe(key)

    monkeypatch.setattr(diagnose, "_probe", probe)
    out = _run(diagnose.run())
    assert out["status"] == "ok" and out["checked"] == len(oracles.KNOWN)
    row = next(r for r in out["oracles"] if r["key"] == victim)
    assert row["status"] == "error" and out["rollup"] == "error"
    assert "RuntimeError: probe blew up" in row["checks"][0]["detail"]


# --- tool wiring -----------------------------------------------------------


def test_tool_is_registered():
    assert server.DIAGNOSE_TOOL_DESCRIPTION
    assert server._TOOL_ANNOTATIONS["diagnose"].readOnlyHint is True
    # no-arg tool, like host_status
    assert server._DIAGNOSE_SCHEMA["properties"] == {}


def test_handler_returns_rollup(monkeypatch):
    _patch_cli(monkeypatch, (True, "ok"))
    out = _run(server._handle_diagnose({}))
    assert out["status"] == "ok" and "rollup" in out and out["oracles"]
