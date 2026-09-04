"""Oracle circuit breaker — auto-skip chronically-failing backends."""

from __future__ import annotations

import time

from ask_fable.health import Breaker


def test_breaker_starts_closed():
    b = Breaker()
    assert b.state("glm") == "closed"
    assert b.should_skip("glm") is False


def test_breaker_opens_after_threshold(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_BREAKER_WINDOW", "5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_THRESHOLD", "0.5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_COOLDOWN", "9999")
    b = Breaker()
    # Need at least _MIN_SAMPLES (5) to trip
    for _ in range(5):
        b.record("glm", "error")
    assert b.state("glm") == "open"
    assert b.should_skip("glm") is True


def test_breaker_does_not_trip_below_min_samples(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_BREAKER_THRESHOLD", "0.1")
    b = Breaker()
    b.record("glm", "error")
    b.record("glm", "error")
    # Only 2 samples, need at least 5
    assert b.state("glm") == "closed"


def test_breaker_does_not_trip_on_refused():
    b = Breaker()
    for _ in range(20):
        b.record("glm", "refused")
    assert b.state("glm") == "closed"


def test_breaker_transitions_to_half_open_after_cooldown(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_BREAKER_WINDOW", "5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_THRESHOLD", "0.5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_COOLDOWN", "1")
    b = Breaker()
    for _ in range(5):
        b.record("glm", "error")
    assert b.state("glm") == "open"
    time.sleep(1.1)
    assert b.state("glm") == "half_open"


def test_breaker_closes_on_success_in_half_open(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_BREAKER_WINDOW", "5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_THRESHOLD", "0.5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_COOLDOWN", "1")
    b = Breaker()
    for _ in range(5):
        b.record("glm", "error")
    assert b.state("glm") == "open"
    time.sleep(1.1)
    assert b.state("glm") == "half_open"
    b.record("glm", "ok")
    assert b.state("glm") == "closed"


def test_breaker_can_be_reset():
    b = Breaker()
    for _ in range(10):
        b.record("glm", "error")
    b.reset("glm")
    assert b.state("glm") == "closed"


def test_breaker_disabled(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CIRCUIT_BREAKER", "0")
    b = Breaker()
    for _ in range(20):
        b.record("glm", "error")
    assert b.state("glm") == "closed"
    assert b.should_skip("glm") is False


def test_breaker_ignores_config_state_errors():
    """not_configured / unknown_oracle describe local config, not backend health —
    they must never trip the breaker (tripping would mask the actionable message)."""
    b = Breaker()
    for _ in range(20):
        b.record("glm", "error", "not_configured")
        b.record("nope", "error", "unknown_oracle")
        b.record("glm", "error", "circuit_open")  # defensive: never self-feed
    assert b.state("glm") == "closed"
    assert b.state("nope") == "closed"


def test_breaker_ignores_auth_failed():
    """A bad API key is chronic until the operator fixes it — tripping the breaker
    would hide the actionable 'fix your key' error behind a misleading circuit_open."""
    b = Breaker()
    for _ in range(20):
        b.record("glm", "error", "auth_failed")
    assert b.state("glm") == "closed"


def test_breaker_close_clears_stale_window(monkeypatch):
    """A half-open probe success must clear the outage-era history — otherwise the
    very next error re-trips instantly over the stale window."""
    monkeypatch.setenv("ASK_FABLE_BREAKER_WINDOW", "10")
    monkeypatch.setenv("ASK_FABLE_BREAKER_THRESHOLD", "0.5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_COOLDOWN", "1")
    b = Breaker()
    for _ in range(6):
        b.record("glm", "error", "timeout")
    assert b.state("glm") == "open"
    time.sleep(1.1)
    assert b.state("glm") == "half_open"
    b.record("glm", "ok")  # probe succeeds -> closed, window cleared
    assert b.state("glm") == "closed"
    # One fresh error must NOT re-trip (only 2 samples now, and rate is 1/2)
    b.record("glm", "error", "timeout")
    assert b.state("glm") == "closed"


def test_record_reports_open_and_close_transitions(monkeypatch):
    """The breaker hands back each state change so the caller can log it — a
    trip nobody can see was the whole "breaker transitions are silent" gap."""
    monkeypatch.setenv("ASK_FABLE_BREAKER_WINDOW", "5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_THRESHOLD", "0.5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_COOLDOWN", "9999")
    b = Breaker()
    seen = [b.record("glm", "error", "timeout") for _ in range(5)]
    assert seen[:4] == [None] * 4
    trip = seen[4]
    assert trip is not None and trip.to == "opened" and trip.key == "glm"
    assert trip.error_rate == 1.0 and trip.samples == 5 and trip.window == 5
    assert trip.cooldown_s == 9999
    # Cooldown elapses → half-open; a probe success closes it and says so.
    b._states["glm"].opened_at -= 10000
    assert b.state("glm") == "half_open"
    closed = b.record("glm", "ok")
    assert closed is not None and closed.to == "closed"
    assert closed.samples == 1 and closed.error_rate == 0.0
    assert b.record("glm", "ok") is None  # steady state: nothing to report


def test_record_reports_failed_probe_as_reopened(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_BREAKER_WINDOW", "5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_THRESHOLD", "0.5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_COOLDOWN", "9999")
    b = Breaker()
    for _ in range(5):
        b.record("glm", "error", "timeout")
    # An error while still OPEN (a caller that passed the check just before the
    # trip) restarts the cooldown but is not a transition.
    assert b.record("glm", "error", "timeout") is None
    b._states["glm"].opened_at -= 10000  # cooldown elapsed → half-open probe
    assert b.state("glm") == "half_open"
    again = b.record("glm", "error", "timeout")
    assert again is not None and again.to == "reopened"
    assert b.state("glm") == "open"


def test_record_reports_nothing_when_disabled_or_non_health(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_BREAKER_WINDOW", "5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_THRESHOLD", "0.5")
    b = Breaker()
    assert all(b.record("glm", "error", "auth_failed") is None for _ in range(6))
    monkeypatch.setenv("ASK_FABLE_CIRCUIT_BREAKER", "0")
    assert all(b.record("m3", "error", "timeout") is None for _ in range(6))


def test_window_shrinks_when_the_setting_shrinks(monkeypatch):
    """One pop per record never shrinks an over-long deque, so a lowered window
    would report "20/5 calls" forever."""
    monkeypatch.setenv("ASK_FABLE_BREAKER_WINDOW", "20")
    monkeypatch.setenv("ASK_FABLE_BREAKER_THRESHOLD", "0.5")
    b = Breaker()
    for _ in range(20):
        b.record("glm", "ok")
    monkeypatch.setenv("ASK_FABLE_BREAKER_WINDOW", "5")
    last = None
    for _ in range(20):
        last = b.record("glm", "error", "timeout") or last
    assert last is not None
    assert last.samples <= last.window == 5
