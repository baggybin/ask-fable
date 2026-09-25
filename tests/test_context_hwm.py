"""Reader-side rollback guard: monotonic per-key high-water marks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ask_fable import context_hwm

S = 1_000_000_000  # one second in ns


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CONTEXT_HWM_PATH", str(tmp_path / "hwm.db"))
    monkeypatch.delenv("ASK_FABLE_CONTEXT_HWM", raising=False)


def test_first_check_records_and_advances():
    context_hwm.check("k", 100 * S)
    assert context_hwm.floor("k") == 100 * S
    context_hwm.check("k", 200 * S)
    assert context_hwm.floor("k") == 200 * S


def test_rollback_beyond_slack_raises_and_keeps_mark():
    context_hwm.check("k", 1000 * S)
    with pytest.raises(context_hwm.RollbackDetected):
        context_hwm.check("k", 1000 * S - 120 * S)  # 120 s older than the mark
    assert context_hwm.floor("k") == 1000 * S  # the rejected value did not move it


def test_disorder_within_slack_is_tolerated():
    context_hwm.check("k", 1000 * S)
    context_hwm.check("k", 1000 * S - 30 * S)  # 30 s older, inside the 60 s window
    assert context_hwm.floor("k") == 1000 * S


def test_record_is_monotonic_and_never_lowers():
    context_hwm.record("k", 1000 * S)
    context_hwm.record("k", 500 * S)  # a skewed/older write must not lower the mark
    assert context_hwm.floor("k") == 1000 * S


def test_clear_drops_the_mark():
    context_hwm.check("k", 1000 * S)
    context_hwm.clear("k")
    assert context_hwm.floor("k") is None
    context_hwm.check("k", 1 * S)  # fresh start after a client-side delete


def test_marks_are_per_key():
    context_hwm.check("a", 1000 * S)
    context_hwm.check("b", 1 * S)  # unrelated key, no floor
    assert context_hwm.floor("a") == 1000 * S and context_hwm.floor("b") == 1 * S


def test_unavailable_store_fails_closed(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("ASK_FABLE_CONTEXT_HWM_PATH", str(blocker / "hwm.db"))
    with pytest.raises(context_hwm.HwmUnavailable):
        context_hwm.check("k", 1000 * S)
    context_hwm.record("k", 1000 * S)  # best-effort on the write path: never raises
    context_hwm.clear("k")  # likewise


def test_disabled_env_skips_the_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CONTEXT_HWM", "0")
    context_hwm.check("k", 1000 * S)
    context_hwm.check("k", 1 * S)  # would be a rollback if the guard were on
    assert not Path(os.environ["ASK_FABLE_CONTEXT_HWM_PATH"]).exists()  # no store created
    assert context_hwm.floor("k") is None
