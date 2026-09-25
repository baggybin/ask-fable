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
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field


def _enabled() -> bool:
    return (os.environ.get("ASK_FABLE_CIRCUIT_BREAKER") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
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


def _quota_hold() -> float:
    """Base seconds a ``rate_limit`` holds an oracle out of the fan-out.

    ``0`` (the default) is the OFF switch: no hold is ever set and any stale hold
    is ignored, so behaviour is byte-identical to a build without this feature —
    a rate_limit just feeds the health window and trips normally. Set it to opt in
    (a quota block resets on a wall clock, so a hold in the minutes is the point)."""
    try:
        return max(0.0, float(os.environ.get("ASK_FABLE_QUOTA_HOLD") or 0.0))
    except (TypeError, ValueError):
        return 0.0


# Upper bound on one hold, so exponential escalation can't strand an oracle for a day.
_QUOTA_HOLD_MAX = 3600.0


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
    "model_too_large",  # caller asked for a model the GPU cannot hold — not a sick backend
    "payment_required",  # HTTP 402: out of credits — chronic until paid, like a bad key
    "budget_exhausted",  # spent the request's whole output budget reasoning — the budget's fault
    "context_too_large",  # prompt too big for a CLI's argv — the request's size, not the backend
    "bad_input",  # the prompt could not be passed to a CLI at all (NUL byte / surrogate)
    # The rest are the same family as `not_configured`: a permanent local or
    # request state that five calls in a row will report identically, so letting
    # them trip the breaker replaces an actionable message ("install codex") with
    # "circuit_open" for the cooldown. `binary_missing` reaches this from the
    # Claude SDK too (CLINotFoundError), so a host with no Claude Code binary and
    # a pinned SDK transport used to take FABLE offline after five councils.
    "binary_missing",  # the CLI isn't installed — install it; nothing is sick
    "sdk_unavailable",  # the Claude Agent SDK isn't importable — same
    "transport_incapable",  # this transport can't carry the call (http follow-up)
    "disabled",  # the operator turned the backend off
    "bad_args",  # the caller/config asked for something impossible
    "model_not_found",  # the named model isn't on this server — not a sick server
    "context_too_small",  # the model's window can't hold the prompt — the request
    "busy",  # a resident model is answering; the swap was refused, not failed
)


@dataclass
class _State:
    outcomes: deque = field(default_factory=deque)  # last N: True=error, False=ok/refused
    opened_at: float = 0.0  # when the breaker tripped (monotonic)
    # Quota hold (populated only when the ASK_FABLE_QUOTA_HOLD feature is on). A
    # rate_limit is "wait until T", distinct from the health window — these fields
    # are inert (always 0) until that feature writes them, and `snapshot` reports
    # them read-only so the diagnose tool can render a gate reason forward-compatibly.
    held_until: float = 0.0  # monotonic; a live hold skips regardless of health state
    hold_step: int = 0  # consecutive holds, for exponential backoff


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
        # Quota hold (opt-in via ASK_FABLE_QUOTA_HOLD). A rate_limit is the provider
        # saying "wait until T", which is NOT the same as "chronically failing": we
        # set a separate, escalating per-oracle hold and DO NOT feed the health
        # window, so `state()` stays pure-health and `diagnose` reads a true signal.
        # Off (base == 0) falls through to the existing logic unchanged.
        hold_base = _quota_hold()
        if is_error and kind == "rate_limit" and hold_base > 0:
            with self._lock:
                s = self._states.setdefault(key, _State())
                now = time.monotonic()
                # Escalate once per expired hold — a burst of concurrent 429s during
                # half-open all see a live hold and only extend it, never re-double.
                if now >= s.held_until:
                    s.hold_step += 1
                hold = min(hold_base * (2 ** (s.hold_step - 1)), _QUOTA_HOLD_MAX)
                hold *= 1.0 + random.uniform(-0.1, 0.1)  # jitter so siblings don't resume together
                s.held_until = max(s.held_until, now + hold)  # monotone: never shortens
            return None  # a hold is not a health transition
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
                # A success proves quota is back — lift any hold and reset escalation.
                s.held_until = 0.0
                s.hold_step = 0
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
        """True when the oracle should be skipped — the breaker is ``open`` OR a
        quota hold is live. Monotone: the hold can only ADD a skip, never remove
        the ``open`` one. The hold is honored only while the feature is on, so with
        it off a stale ``held_until`` is ignored and behaviour is exactly as before.

        ``state()`` and the hold are read in SEPARATE lock acquisitions on purpose:
        ``self._lock`` is a plain (non-reentrant) ``Lock``, so calling ``state()``
        while holding it would deadlock."""
        if self.state(key) == "open":
            return True
        if _quota_hold() <= 0:
            return False
        with self._lock:
            s = self._states.get(key)
            return s is not None and time.monotonic() < s.held_until

    def snapshot(self, key: str) -> dict:
        """Read-only view of the gate for one oracle — for the ``diagnose`` tool.

        NEVER mutates and never records an outcome, so a health check cannot
        perturb the breaker it reports on. Returns a stable ``gate`` shape:
        ``state`` (closed|open|half_open), ``skip_reason`` (``circuit_open`` when
        open, ``quota_hold`` when a quota hold is live but the breaker is not open,
        else ``None``), ``resume_in_s`` (seconds until the open cooldown elapses or
        the hold lifts, whichever is later), plus the raw ``held_until``/
        ``hold_step``/``window`` for callers that want them. ``skip_reason`` can
        only be ``circuit_open`` until the quota-hold feature is enabled."""
        now = time.monotonic()
        with self._lock:
            s = self._states.get(key)
            if s is None:
                return {
                    "state": "closed",
                    "skip_reason": None,
                    "resume_in_s": None,
                    "held_until": 0.0,
                    "hold_step": 0,
                    "window": 0,
                }
            opened_at = s.opened_at
            held_until = s.held_until
            hold_step = s.hold_step
            window = len(s.outcomes)
        cooldown = _cooldown()
        # State mirrors `state()` (which reports closed while the breaker is disabled).
        if not _enabled() or opened_at == 0.0:
            state = "closed"
        else:
            state = "half_open" if (now - opened_at) >= cooldown else "open"
        held = held_until > now
        skip_reason = "circuit_open" if state == "open" else ("quota_hold" if held else None)
        resume: float | None = None
        if state == "open":
            resume = max(0.0, cooldown - (now - opened_at))
        if held:
            hold_remaining = held_until - now
            resume = hold_remaining if resume is None else max(resume, hold_remaining)
        return {
            "state": state,
            "skip_reason": skip_reason,
            "resume_in_s": round(resume, 1) if resume is not None else None,
            "held_until": held_until,
            "hold_step": hold_step,
            "window": window,
        }

    def reset(self, key: str) -> None:
        """Clear the breaker for ``key`` (e.g. after a manual operator action)."""
        with self._lock:
            self._states.pop(key, None)


breaker = Breaker()
