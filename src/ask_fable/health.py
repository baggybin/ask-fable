"""Per-oracle circuit breaker — auto-skip chronically-failing backends.

A lightweight in-memory health tracker that records the last N outcomes per oracle
key. When the error rate over the recent window exceeds a threshold (AND we have
enough samples), the oracle is marked ``open`` and skipped in council fan-out —
reported as ``circuit_open`` in the ``sources`` dict, exactly like
``not_configured``. After a cooldown, one ``half_open`` probe call is allowed; a
success closes the breaker, a failure re-opens it.

Never trips on ``refused`` (a scope decision is not a failure), and never on
permanent CONFIG states (``not_configured`` / ``unknown_oracle`` — there is no
backend to protect, and tripping would mask the actionable "set the key" message
behind a misleading breaker error). Thread-safe via a ``threading.Lock`` (same
pattern as ``SessionStore``). Process-local — resets on restart, which is fine
since the goal is short-term protection against a flaky backend, not a
persistent health record.

Half-open is deliberately NOT single-probe: once the cooldown elapses, every
caller passes through until one outcome is recorded (a council fan-out may send
a few concurrent probes). At this server's stated low concurrency that's an
acceptable trade for simplicity — don't assume classic one-token semantics.

The breaker does no I/O of its own: ``record`` hands back a ``Transition`` for
each state change and the caller (``oracles._report_breaker``) puts it on the
console and in the trace log.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field


def _enabled() -> bool:
    return (os.environ.get("ASK_FABLE_CIRCUIT_BREAKER") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _window() -> int:
    try:
        return max(1, int(os.environ.get("ASK_FABLE_BREAKER_WINDOW") or 20))
    except (TypeError, ValueError):
        return 20


def _threshold() -> float:
    try:
        return max(0.1, min(1.0, float(os.environ.get("ASK_FABLE_BREAKER_THRESHOLD") or 0.5)))
    except (TypeError, ValueError):
        return 0.5


def _cooldown() -> float:
    try:
        return max(1.0, float(os.environ.get("ASK_FABLE_BREAKER_COOLDOWN") or 300.0))
    except (TypeError, ValueError):
        return 300.0


_MIN_SAMPLES = 5  # don't trip on the first few calls

# Error kinds that say nothing about backend HEALTH — permanent local config
# states (nothing to protect; tripping would mask the actionable message),
# auth failures (a bad key is chronic until the operator fixes it — tripping
# would hide the actionable "fix your key" error behind "circuit_open"), and
# the breaker's own synthetic result (defensive: one refactor away from a
# self-feeding breaker if run() ever records it).
_NON_HEALTH_KINDS = (
    "not_configured",
    "unknown_oracle",
    "auth_failed",
    "circuit_open",
    "model_unavailable",
)


@dataclass
class _State:
    outcomes: deque = field(default_factory=deque)  # last N: True=error, False=ok/refused
    opened_at: float = 0.0  # when the breaker tripped (monotonic)


@dataclass(frozen=True)
class Transition:
    """One breaker state change, handed back from ``Breaker.record`` so the caller
    — which owns the trace scope and the console — can report it; the breaker
    itself stays a leaf with no I/O. ``to`` is ``opened`` (closed → open),
    ``reopened`` (a half-open probe failed) or ``closed`` (a probe succeeded).
    ``error_rate``/``samples`` describe the window as it stands after the change —
    for ``closed`` that is the single fresh success that seeded it. ``probe`` says
    whether a ``closed`` came from a real half-open probe or from a straggler that
    was already in flight when the breaker tripped — both close it, but only one
    is evidence the backend recovered."""

    key: str
    to: str
    probe: bool
    error_rate: float
    samples: int
    window: int
    cooldown_s: float


class Breaker:
    """Thread-safe per-oracle circuit breaker."""

    def __init__(self) -> None:
        self._states: dict[str, _State] = {}
        self._lock = threading.Lock()

    def record(self, key: str, status: str, kind: str = "") -> Transition | None:
        """Record an oracle outcome. ``error`` counts as a failure; everything
        else (``ok``, ``refused``) counts as a success. A success during
        ``half_open`` closes the breaker AND clears the error window — otherwise
        the stale outage-era history would re-trip on the very next error,
        defeating the probe's purpose (a recovered-but-once-flaky backend gets
        ``_MIN_SAMPLES`` fresh calls before it can trip again; that's the price).

        Errors whose ``kind`` is in ``_NON_HEALTH_KINDS`` are ignored entirely —
        they describe local configuration, not backend health.

        Returns the state change this outcome caused, or ``None`` when the
        breaker stayed put. An error that lands while the breaker is still
        ``open`` (a caller that passed the check just before the trip) restarts
        the cooldown as before but is not a transition. With the breaker
        disabled the window is still tracked but nothing is reported — a "trip"
        that skips no one is not news."""
        if status == "error" and kind in _NON_HEALTH_KINDS:
            return None
        is_error = status == "error"
        transition: Transition | None = None
        with self._lock:
            s = self._states.setdefault(key, _State())
            window = _window()
            # ``while``, not ``if``: one pop per record never shrinks a deque that
            # is already longer than a freshly-lowered ASK_FABLE_BREAKER_WINDOW,
            # and the reported "N/window calls" would read 20/5 forever.
            while len(s.outcomes) >= window:
                s.outcomes.popleft()
            s.outcomes.append(is_error)
            was_open = s.opened_at != 0.0
            probing = was_open and (time.monotonic() - s.opened_at) >= _cooldown()
            if not is_error:
                if was_open:
                    # Recovering from open/half-open: clear the stale window so
                    # one fresh error can't instantly re-trip over old history.
                    s.outcomes.clear()
                    s.outcomes.append(False)  # keep this success as the seed sample
                    transition = self._transition(key, "closed", s, window, probe=probing)
                s.opened_at = 0.0
            # Check if we should trip. Skipped entirely while disabled: the trip
            # would go unreported (the Transition is suppressed below), and then
            # flipping ASK_FABLE_CIRCUIT_BREAKER back on mid-process would start
            # shedding from a state change that never reached a log or a console.
            elif _enabled() and len(s.outcomes) >= _MIN_SAMPLES:
                err_rate = sum(s.outcomes) / len(s.outcomes)
                if err_rate >= _threshold():
                    s.opened_at = time.monotonic()
                    if not was_open:
                        transition = self._transition(key, "opened", s, window, probe=False)
                    elif probing:
                        transition = self._transition(key, "reopened", s, window, probe=True)
        return transition if _enabled() else None

    @staticmethod
    def _transition(key: str, to: str, s: _State, window: int, *, probe: bool) -> Transition:
        samples = len(s.outcomes)
        return Transition(
            key=key,
            to=to,
            probe=probe,
            error_rate=round(sum(s.outcomes) / samples, 3) if samples else 0.0,
            samples=samples,
            window=window,
            cooldown_s=_cooldown(),
        )

    def state(self, key: str) -> str:
        """Return ``closed`` | ``open`` | ``half_open`` for this oracle.

        - ``closed``: normal operation.
        - ``open``: error rate exceeded the threshold; the oracle should be skipped.
        - ``half_open``: cooldown elapsed; allow one probe call to test recovery.
        """
        if not _enabled():
            return "closed"
        with self._lock:
            s = self._states.get(key)
            if s is None or s.opened_at == 0.0:
                return "closed"
            elapsed = time.monotonic() - s.opened_at
            if elapsed >= _cooldown():
                return "half_open"
            return "open"

    def should_skip(self, key: str) -> bool:
        """True when the oracle should be skipped (breaker is ``open``)."""
        return self.state(key) == "open"

    def reset(self, key: str) -> None:
        """Clear the breaker for ``key`` (e.g. after a manual operator action)."""
        with self._lock:
            self._states.pop(key, None)


breaker = Breaker()
