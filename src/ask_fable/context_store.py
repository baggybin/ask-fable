"""A tiny durable keyed context store — the "mini MCP bus" blackboard.

The point is to kill the re-paste tax: the model can't see the repo, so today an
agent re-pastes the same big code context into every ``ask``. Here it can
``context_write('repo:auth', <big code>)`` ONCE and then pass ``context_ref`` on
later calls (or ``context_read`` it, or let a sibling agent read it) — one shared
blob instead of N copies. This is salient-core's "shared bootstrap context" idea,
reimplemented dependency-free and scoped to ask_fable's actual bottleneck.

Storage mirrors ``cache.py``: one best-effort SQLite file (0600) at
``$ASK_FABLE_CONTEXT_PATH`` or ``${XDG_STATE_HOME:-~/.local/state}/ask_fable/
context.db``. Every op is best-effort — any sqlite/OS error degrades to
None/False, never raises. Values persist until overwritten or deleted (no TTL):
a stored context is an explicit artifact, not a cache entry.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

from . import _paths


def _db_path() -> Path:
    override = os.environ.get("ASK_FABLE_CONTEXT_PATH")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_state_dir() / "ask_fable" / "context.db"


# Best-effort ops never raise, but ``get() -> None`` then conflates "key absent" with
# "DB unreachable/corrupt". We stash the last failure reason here (and clear it on any
# successful connect) so tool handlers can surface a degraded store instead of telling
# the agent its keys don't exist — which would make it re-paste the very context the
# store exists to hold. Not thread-synchronized: last-writer-wins is fine for a hint.
_LAST_ERROR: str | None = None


def last_error() -> str | None:
    """Most recent store failure reason, or None if the last connection was clean."""
    return _LAST_ERROR


def db_path() -> str:
    """Resolved on-disk store path, for diagnostics in a degraded-store response."""
    return str(_db_path())


def _note_error(exc: object) -> None:
    global _LAST_ERROR
    _LAST_ERROR = f"{type(exc).__name__}: {exc}"


def _clear_error() -> None:
    global _LAST_ERROR
    _LAST_ERROR = None


def _connect() -> sqlite3.Connection | None:
    try:
        p = _db_path()
        # Pre-create at 0o600 to avoid the world-readable TOCTOU window.
        if not _paths.ensure_dir_secure(p.parent):
            _note_error(OSError(f"state dir not writable: {p.parent}"))
            return None
        if not p.exists():
            try:
                fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
            except OSError as exc:
                _note_error(exc)
                return None
        conn = sqlite3.connect(str(p), timeout=2.0)
        # WAL + synchronous=NORMAL: crash-safe without the latency of FULL.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
    except Exception as exc:  # noqa: BLE001 — the store must never break a tool call
        _note_error(exc)
        return None
    try:  # a failure here (locked/corrupt/full) must still close the open handle
        conn.execute(
            "CREATE TABLE IF NOT EXISTS context "
            "(key TEXT PRIMARY KEY, value TEXT, ts REAL, description TEXT)"
        )
        _paths.secure_sqlite_sidecars(p)
        _clear_error()  # clean connection — clear any stale failure from a prior call
        return conn
    except Exception as exc:  # noqa: BLE001
        _note_error(exc)
        conn.close()
        return None


def put(key: str, value: str, description: str = "") -> bool:
    """Store (or overwrite) ``value`` under ``key``. Returns True on success."""
    key = (key or "").strip()
    if not key:
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        conn.execute(
            "INSERT OR REPLACE INTO context (key, value, ts, description) VALUES (?, ?, ?, ?)",
            (key, value or "", time.time(), (description or "").strip()),
        )
        conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        _note_error(exc)  # connect succeeded but the write failed (locked/full DB) —
        print(f"ask_fable: context write failed: {exc}", file=sys.stderr)  # else invisible
        return False
    finally:
        conn.close()


def get(key: str) -> str | None:
    """Return the stored value for ``key``, or None if absent."""
    row = _row(key)
    return None if row is None else row[0]


def get_meta(key: str) -> tuple[str, float, str] | None:
    """Return (value, ts, description) for ``key``, or None if absent."""
    return _row(key)


def _row(key: str) -> tuple[str, float, str] | None:
    key = (key or "").strip()
    if not key:
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT value, ts, description FROM context WHERE key = ?", (key,)
        ).fetchone()
        # Coerce nullable columns so callers (and get_meta consumers) never see None.
        return (row[0] or "", float(row[1] or 0.0), row[2] or "") if row else None
    except Exception as exc:  # noqa: BLE001
        _note_error(exc)  # SELECT failed on an open handle — likely corrupt/locked DB
        return None
    finally:
        conn.close()


def delete(key: str) -> bool:
    """Delete ``key``. Returns True if a row was removed."""
    key = (key or "").strip()
    if not key:
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        cur = conn.execute("DELETE FROM context WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0
    except Exception as exc:  # noqa: BLE001
        _note_error(exc)
        return False
    finally:
        conn.close()


def entries() -> list[dict]:
    """List stored keys with size/age/description (never the full value) so an
    agent can discover what the bus holds. Newest first. Best-effort → []."""
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT key, length(value), ts, description FROM context ORDER BY ts DESC"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        _note_error(exc)
        return []
    finally:
        conn.close()
    now = time.time()
    return [
        {"key": r[0], "bytes": int(r[1] or 0), "age_s": max(0, int(now - (r[2] or now))),
         "description": r[3] or ""}
        for r in rows
    ]
