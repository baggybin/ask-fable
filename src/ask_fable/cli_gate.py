"""Shared subprocess runner + per-binary concurrency gate for local CLI oracles
(claude, mmx, grok, codex, agy).

Council fan-out can spawn many subprocess bridges at once. Some CLIs share
account quotas / process-global state and fail opaquely under load. This module
caps concurrent processes **per binary name** with a thread-safe semaphore so
``asyncio.to_thread`` workers serialize correctly across event loops — and owns
the one canonical spawn/kill protocol every bridge previously copy-pasted:
own-session spawn, group-SIGKILL on timeout, and a bounded post-kill drain.

Tune with ``ASK_FABLE_CLI_MAX_PARALLEL`` (default 2). Set to 0 or a negative
value to disable the gate. Set to 1 to fully serialize a given CLI family.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

_gates: dict[str, threading.Semaphore] = {}
_gates_lock = threading.Lock()


class GateBusy(Exception):
    """Raised by :func:`hold` when no slot frees up within its ``timeout``."""


def _limit() -> int:
    raw = (os.environ.get("ASK_FABLE_CLI_MAX_PARALLEL") or "").strip()
    if not raw:
        return 2
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 2
    return n  # 0 or negative → disabled


def _semaphore(name: str) -> threading.Semaphore | None:
    limit = _limit()
    if limit <= 0:
        return None
    key = (name or "cli").strip() or "cli"
    with _gates_lock:
        sem = _gates.get(key)
        if sem is None:
            # Recreate if limit env changes mid-process is not supported; first
            # use of a binary freezes its slot count for this process.
            sem = threading.Semaphore(limit)
            _gates[key] = sem
        return sem


@contextmanager
def hold(name: str, timeout: float | None = None) -> Iterator[None]:
    """Acquire a slot for CLI binary ``name``. No-op if the gate is disabled.

    Without ``timeout`` the acquire blocks indefinitely (legacy behavior).
    With ``timeout``, raises :class:`GateBusy` when no slot frees in time —
    an untimed queue wait would make a call's wall time unbounded by its own
    timeout, and a caller cancelled while parked in an untimed acquire would
    still launch a CLI run nobody reads."""
    sem = _semaphore(name)
    if sem is None:
        yield
        return
    if timeout is None:
        sem.acquire()
    elif not sem.acquire(timeout=timeout):
        raise GateBusy(name)
    try:
        yield
    finally:
        sem.release()


@dataclass
class CliRun:
    """Outcome of one gated CLI run. ``queue_timed_out`` means the whole
    budget was spent waiting for a gate slot — no subprocess was spawned."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    queue_timed_out: bool = False


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the whole process group (the CLI + any children), falling back
    to the leader alone if the group is already gone."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


class _ProcHandle:
    """Bridge between the (uncancellable) ``to_thread`` worker running the child and
    the async caller that may be cancelled while it runs. ``asyncio.to_thread`` work
    can't be cancelled, so before this the child kept running to its own inner timeout
    after a council cap fired — a live-process + busy-thread leak window. The worker
    publishes its ``proc`` here once spawned; on cancel the caller calls ``kill`` and
    the child's group is SIGKILLed instead of orphaned. Locked so a kill racing the
    spawn either kills the just-spawned child (set() returns False → don't run it) or
    the recorded one — never neither."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._killed = False

    def set(self, proc: subprocess.Popen) -> bool:
        """Record the spawned child. Returns False if the caller already cancelled —
        the child is killed here and the worker must not run it."""
        with self._lock:
            if self._killed:
                _kill_group(proc)
                return False
            self._proc = proc
            return True

    def kill(self) -> None:
        with self._lock:
            self._killed = True
            if self._proc is not None:
                _kill_group(self._proc)


def run_cli(
    argv: list[str],
    *,
    gate: str,
    timeout: float,
    input_text: str | None = None,
    cancelled: threading.Event | None = None,
    env: dict[str, str] | None = None,
    proc_handle: _ProcHandle | None = None,
) -> CliRun:
    """Run ``argv`` under the per-binary gate — the one spawn/kill protocol
    shared by every CLI bridge (claude/mmx/agy/codex/grok). Blocking; use
    :func:`run_cli_async` from async code.

    ``timeout`` is a TOTAL budget: time spent queued on the gate is subtracted
    from the time the subprocess may run, so a call's wall time is bounded by
    ~``timeout`` + the 5s drain regardless of queue depth. The subprocess gets
    its own session; on timeout the whole process group is SIGKILLed and the
    pipes drained for up to 5s (``subprocess.run``'s post-kill reap blocks
    forever if a grandchild holds the stdout pipe — and an agentic CLI turn
    would then never release its gate slot, deadlocking every later call).

    ``input_text`` selects the stdin mode: a string is piped to stdin
    (claude/mmx read their prompt there); ``None`` closes stdin to DEVNULL so
    the child can't inherit — and hang on — the MCP server's JSON-RPC pipe.

    ``cancelled`` is checked after the slot is acquired, before spawning: a
    caller cancelled while queued (e.g. by the council wall-clock cap) must
    not launch a CLI run whose result is discarded — pure quota burn.

    ``env`` OVERLAYS the server's environment for this child only (the kimi
    bridge points ``KIMI_CODE_HOME`` at its sandboxed config root this way).
    It is a merge, not a replacement: a CLI that needs HOME/PATH to find its
    own login keeps them.
    """
    deadline = time.monotonic() + timeout
    try:
        with hold(gate, timeout=timeout):
            if cancelled is not None and cancelled.is_set():
                return CliRun(None, "", "", timed_out=False)
            remaining = max(deadline - time.monotonic(), 0.001)
            # Popen itself can fail before the child exists — E2BIG when an argv
            # element exceeds MAX_ARG_STRLEN, ENOMEM, EACCES. Bridges document
            # "never raises for expected failures", so report it as a failed run
            # instead of letting it escape past every handler to the tool's
            # catch-all (which also skips the health breaker).
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    # errors="replace": a child emitting raw/truncated bytes must not
                    # raise UnicodeDecodeError out of communicate() and skip kill+reap.
                    errors="replace",
                    start_new_session=True,  # own process group → we can signal the tree
                    env={**os.environ, **env} if env else None,
                )
            except OSError as exc:
                return CliRun(None, "", f"failed to start {argv[0]}: {exc}", timed_out=False)
            # Publish the child so an async caller cancelled mid-run can kill it (L1).
            # If it was already cancelled, set() killed it and we must not run it.
            if proc_handle is not None and not proc_handle.set(proc):
                return CliRun(None, "", "", timed_out=False)
            try:
                out, err = proc.communicate(input=input_text, timeout=remaining)
                return CliRun(proc.returncode, out or "", err or "", False)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                try:
                    out, err = proc.communicate(timeout=5)  # drain pipes / reap the leader
                except subprocess.TimeoutExpired:
                    out, err = "", ""
                return CliRun(None, out or "", err or "", True)
            except Exception as exc:  # noqa: BLE001 — never orphan, never escape
                # Any other mid-run failure (pipe OSError, etc.): SIGKILL the group and
                # reap, so we never leave an orphan and never break the bridges'
                # "never raises for expected failures" contract.
                _kill_group(proc)
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                return CliRun(
                    None, "", f"{argv[0]} failed mid-run: {type(exc).__name__}: {exc}",
                    timed_out=False,
                )
    except GateBusy:
        return CliRun(None, "", "", timed_out=True, queue_timed_out=True)


async def run_cli_async(
    argv: list[str],
    *,
    gate: str,
    timeout: float,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> CliRun:
    """Async entry for :func:`run_cli`. ``asyncio.to_thread`` work itself cannot be
    cancelled, so on cancel we (1) flag ``cancelled`` — a worker still queued on the
    gate never spawns the subprocess — and (2) ``handle.kill()`` — a child already
    spawned is SIGKILLed with its group rather than left running to its own inner
    timeout (a live-process + busy-thread leak window a council cap used to open)."""
    cancelled = threading.Event()
    handle = _ProcHandle()
    try:
        return await asyncio.to_thread(
            run_cli,
            argv,
            gate=gate,
            timeout=timeout,
            input_text=input_text,
            cancelled=cancelled,
            env=env,
            proc_handle=handle,
        )
    except asyncio.CancelledError:
        cancelled.set()  # a not-yet-spawned child never launches
        handle.kill()  # an already-spawned child is SIGKILLed, not left running to timeout
        raise
