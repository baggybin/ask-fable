"""Quota-aware hold in the circuit breaker (B).

A `rate_limit` is "wait until T", not "unhealthy": when ASK_FABLE_QUOTA_HOLD is on
it sets a separate escalating per-oracle hold and does NOT feed the health window,
so `state()` stays pure-health. Off (the default) it behaves exactly as before.

All timing is driven by an injected clock and zeroed jitter — no real sleeps.
"""

from __future__ import annotations

import pytest

import ask_fable.health as health
from ask_fable.health import Breaker


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(health.time, "monotonic", c)
    monkeypatch.setattr(health.random, "uniform", lambda a, b: 0.0)  # deterministic holds
    return c


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUOTA_HOLD", "100")


@pytest.fixture
def off(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_QUOTA_HOLD", raising=False)


# --- flag off: zero behaviour change ---------------------------------------


def test_flag_off_rate_limit_feeds_window_like_any_error(clock, off):
    b = Breaker()
    for _ in range(health._MIN_SAMPLES):
        b.record("k", "error", kind="rate_limit")
    assert b.state("k") == "open"  # trips normally — identical to today
    assert b._states["k"].held_until == 0.0


def test_flag_off_ignores_a_stale_hold(clock, monkeypatch):
    b = Breaker()
    # A hold set while the feature was on...
    monkeypatch.setenv("ASK_FABLE_QUOTA_HOLD", "100")
    b.record("k", "error", kind="rate_limit")
    assert b.should_skip("k") is True
    # ...is ignored the moment the feature is turned off.
    monkeypatch.delenv("ASK_FABLE_QUOTA_HOLD")
    assert b.should_skip("k") is False


@pytest.mark.parametrize("kind", ["network_error", "sdk_error", "timeout", "", "mystery_kind"])
def test_non_rate_limit_is_identical_on_and_off(clock, monkeypatch, kind):
    def run(flag: str | None) -> list[str]:
        if flag is None:
            monkeypatch.delenv("ASK_FABLE_QUOTA_HOLD", raising=False)
        else:
            monkeypatch.setenv("ASK_FABLE_QUOTA_HOLD", flag)
        c = _Clock()
        monkeypatch.setattr(health.time, "monotonic", c)
        b = Breaker()
        states = []
        for _ in range(8):
            b.record("k", "error", kind=kind)
            states.append(b.state("k"))
        return states

    assert run("100") == run(None)  # the hold branch never fires for non-rate_limit


# --- flag on: the hold ------------------------------------------------------


def test_rate_limit_holds_without_feeding_the_window(clock, on):
    b = Breaker()
    for _ in range(10):  # far past _MIN_SAMPLES
        assert b.record("k", "error", kind="rate_limit") is None  # not a health transition
    assert b.state("k") == "closed"  # window never fed -> pure-health stays closed
    assert len(b._states["k"].outcomes) == 0
    assert b.should_skip("k") is True  # but skipped by the hold
    assert b._states["k"].held_until == clock.t + 100


def test_hold_expires(clock, on):
    b = Breaker()
    b.record("k", "error", kind="rate_limit")
    assert b.should_skip("k") is True
    clock.advance(99)
    assert b.should_skip("k") is True
    clock.advance(2)  # past 100
    assert b.should_skip("k") is False


def test_hold_escalates_once_per_expiry(clock, on):
    b = Breaker()
    b.record("k", "error", kind="rate_limit")
    assert b._states["k"].hold_step == 1 and b._states["k"].held_until == clock.t + 100
    # a second 429 while still held: no re-escalation, hold not shortened
    b.record("k", "error", kind="rate_limit")
    assert b._states["k"].hold_step == 1 and b._states["k"].held_until == clock.t + 100
    # after it expires and it's STILL limited: escalate (doubling)
    clock.advance(101)
    b.record("k", "error", kind="rate_limit")
    assert b._states["k"].hold_step == 2 and b._states["k"].held_until == clock.t + 200


def test_success_clears_the_hold(clock, on):
    b = Breaker()
    b.record("k", "error", kind="rate_limit")
    assert b.should_skip("k") is True
    b.record("k", "ok")
    assert b._states["k"].held_until == 0.0 and b._states["k"].hold_step == 0
    assert b.should_skip("k") is False


def test_hold_never_shortens(clock, on):
    b = Breaker()
    b._states["k"] = health._State(held_until=clock.t + 5000)  # a long hold already set
    b.record("k", "error", kind="rate_limit")  # would compute a shorter (100s) hold
    assert b._states["k"].held_until == clock.t + 5000  # max() kept the longer one


def test_snapshot_shows_quota_hold(clock, on):
    b = Breaker()
    b.record("k", "error", kind="rate_limit")
    snap = b.snapshot("k")
    assert snap["state"] == "closed" and snap["skip_reason"] == "quota_hold"
    assert snap["resume_in_s"] == 100.0
