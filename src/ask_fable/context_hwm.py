"""Reader-side rollback guard for the LAN context bus — a per-key high-water mark.

The daemon's persistent ``writer_ts`` floor stops an *older blob from being
written*, but a reader still has no way to tell that an owner is serving an
envelope that *is* validly sealed yet should no longer be current — a restored
backup, a rolled-back database, or a malicious owner replaying a captured blob.
Because ``ts_ns`` lives in the AEAD-bound envelope header, a reader that
remembers the newest timestamp it has ever accepted per key can catch that: a
served envelope more than the skew slack older than the mark is refused.

Design:

- **Local, per-machine state.** The mark is never stored on the bus (a rolled-back
  owner could roll that back too). It lives in its own tiny SQLite file
  (``ASK_FABLE_CONTEXT_HWM_PATH`` or ``~/.local/state/ask_fable/context_hwm.db``,
  0600/WAL, opened per operation like the other stores), keyed by storage key.
- **Monotonic.** Every accepted read/write advances the mark with
  ``MAX(existing, observed)``; it never goes backwards.
- **Slack, not strict.** A value within ``FLOOR_SLACK_NS`` (60 s, the same window
  the daemon's write floor uses) of the mark is accepted — clocks across machines
  are not synchronized, and the protocol already bounds disorder to that window.
- **Fail closed.** If the guard store cannot be read, the read degrades with a
  named error rather than skipping the check (an adversary-inducible failure must
  not collapse into "allowed"). Operators who deliberately restored an older bus
  database can disable the guard with ``ASK_FABLE_CONTEXT_HWM=0``, or reset it by
  deleting the guard database.
- **Best-effort writes.** After a successful bus write the mark is advanced, but
  a guard failure there is a warning only — the write itself succeeded and the
  next read will re-establish the mark.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

from . import _paths, context_bus

SLACK_NS = context_bus.FLOOR_SLACK_NS


class HwmError(RuntimeError):
    """Base class for rollback-guard failures."""


class HwmUnavailable(HwmError):
    """The guard store could not be read/written — fail the read closed."""


class RollbackDetected(HwmError):
    """The served value is older (beyond the slack) than the local high-water mark."""


def _db_path() -> Path:
    override = os.environ.get("ASK_FABLE_CONTEXT_HWM_PATH")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_state_dir() / "ask_fable" / "context_hwm.db"


def _enabled() -> bool:
    return (os.environ.get("ASK_FABLE_CONTEXT_HWM") or "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _connect() -> sqlite3.Connection | None:
    try:
        p = _db_path()
        if not _paths.ensure_dir_secure(p.parent):
            return None
        if not p.exists():
            try:
                fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
            except OSError:
                return None
        conn = sqlite3.connect(str(p), timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        conn.execute(
            "CREATE TABLE IF NOT EXISTS hwm (key TEXT PRIMARY KEY, ts_ns INTEGER, updated REAL)"
        )
        _paths.secure_sqlite_sidecars(p)
        return conn
    except Exception:
        return None


def _bump(conn: sqlite3.Connection, key: str, ts_ns: int) -> None:
    conn.execute(
        "INSERT INTO hwm (key, ts_ns, updated) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "ts_ns = MAX(hwm.ts_ns, excluded.ts_ns), updated = excluded.updated",
        (key, int(ts_ns), time.time()),
    )
    conn.commit()


def check(key: str, ts_ns: int) -> None:
    """Guard one read: refuse a value older than the local mark (beyond slack),
    otherwise advance the mark. Raises ``RollbackDetected`` / ``HwmUnavailable``."""
    if not _enabled():
        return
    conn = _connect()
    if conn is None:
        raise HwmUnavailable(f"rollback guard store unavailable: {_db_path()}")
    try:
        row = conn.execute("SELECT ts_ns FROM hwm WHERE key = ?", (key,)).fetchone()
        if row is not None:
            mark = int(row[0] or 0)
            if int(ts_ns) < mark - SLACK_NS:
                raise RollbackDetected(
                    f"rollback guard: '{key}' was served with ts={int(ts_ns)}, older than "
                    f"the high-water mark {mark} (possible stale or rolled-back owner); "
                    "refusing the value. If this is an intentional restore, set "
                    "ASK_FABLE_CONTEXT_HWM=0 or delete the guard database"
                )
        _bump(conn, key, ts_ns)
    except (RollbackDetected, HwmUnavailable):
        raise
    except Exception as exc:
        raise HwmUnavailable(f"rollback guard store failed: {exc}") from exc
    finally:
        conn.close()


def record(key: str, ts_ns: int) -> None:
    """Advance the mark after a successful write. Best-effort — never raises."""
    if not _enabled():
        return
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect()
        if conn is None:
            raise HwmUnavailable(f"rollback guard store unavailable: {_db_path()}")
        _bump(conn, key, ts_ns)
    except Exception as exc:  # noqa: BLE001 — a guard hint must not fail the write
        print(f"ask_fable: rollback guard record failed: {exc}", file=sys.stderr)
    finally:
        if conn is not None:
            conn.close()


def clear(key: str) -> None:
    """Drop the mark for a key this client just deleted. Best-effort."""
    if not _enabled():
        return
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect()
        if conn is None:
            return
        conn.execute("DELETE FROM hwm WHERE key = ?", (key,))
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        if conn is not None:
            conn.close()


def floor(key: str) -> int | None:
    """Current mark for a key (diagnostics/tests), or None when absent/unavailable."""
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT ts_ns FROM hwm WHERE key = ?", (key,)).fetchone()
        return int(row[0]) if row is not None else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()
