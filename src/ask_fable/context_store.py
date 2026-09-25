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

**Backend seam (LAN bus, step 2 of the 2026-09-12 plan).** The public functions
are a thin shim over one of two backends:

- ``_LocalSqliteBackend`` — the on-disk SQLite store above (default, unchanged).
- ``context_bus.RemoteBusBackend`` — when ``ASK_FABLE_CONTEXT_BUS`` (or the
  ``context_bus`` config key) is set, blobs are sealed client-side and the
  daemon stores only ciphertext. Every transport failure maps to
  ``last_error()`` (degraded) — a configured bus never silently falls back to
  the local store.

**Rollback guard.** Whenever a stored value is an ``afctx1:`` envelope
(``context_crypto``), the shim unseals it with the keyring instead of returning
it raw — in *both* modes. Without a keyring the read degrades (``last_error``
names the sealed key) rather than splicing base64 armor into a prompt. This is
defense-in-depth: the daemon owns its own database, so envelopes should only
appear locally through migration or misconfiguration.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Protocol

from . import _paths, context_bus, context_crypto, context_hwm


def _db_path() -> Path:
    override = os.environ.get("ASK_FABLE_CONTEXT_PATH")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_state_dir() / "ask_fable" / "context.db"


# Best-effort ops never raise, but ``get() -> None`` then conflates "key absent" with
# "DB unreachable/corrupt". We stash the last failure reason here (and clear it at the
# start of every public store operation, in both modes) so tool handlers can surface a
# degraded store instead of telling the agent its keys don't exist — which would make it
# re-paste the very context the store exists to hold. Not thread-synchronized:
# last-writer-wins is fine for a hint.
_LAST_ERROR: str | None = None

# Row cap for the context store. Default 0 = UNLIMITED: a stored context is an
# explicit artifact (no TTL by design), so the default preserves that. An operator
# who wants a disk bound sets ASK_FABLE_CONTEXT_MAX_ROWS>0; the oldest rows beyond
# the cap are then evicted (newest kept), swept every ~100 writes like cache.py.
_sweep_counter = 0


def _max_rows() -> int:
    try:
        return int(os.environ.get("ASK_FABLE_CONTEXT_MAX_ROWS") or 0)
    except (TypeError, ValueError):
        return 0


def _sweep(conn: sqlite3.Connection) -> None:
    """Evict the oldest rows beyond the cap (to 90% of it). No-op when unlimited."""
    cap = _max_rows()
    if cap <= 0:
        return
    try:
        count = conn.execute("SELECT COUNT(*) FROM context").fetchone()[0]
        if count > cap:
            evict = count - int(cap * 0.9)
            conn.execute(
                "DELETE FROM context WHERE key IN "
                "(SELECT key FROM context ORDER BY ts ASC LIMIT ?)",
                (evict,),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        _note_error(exc)


def last_error() -> str | None:
    """Most recent store failure reason, or None if the last connection was clean."""
    return _LAST_ERROR


def _note_error(exc: object) -> None:
    global _LAST_ERROR
    _LAST_ERROR = f"{type(exc).__name__}: {exc}"


def _clear_error() -> None:
    global _LAST_ERROR
    _LAST_ERROR = None


def _map_failure(exc: object) -> None:
    """Single mapping point from any failure (exception or status string) to the
    degraded-store signal. Callers on the read path use this so a sealed blob that
    cannot be opened can never collapse into "missing" (which would let a caller
    proceed without the context it asked for)."""
    if isinstance(exc, BaseException):
        _note_error(exc)
    else:
        global _LAST_ERROR
        _LAST_ERROR = str(exc)


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
            "(key TEXT PRIMARY KEY, value TEXT, ts REAL, description TEXT, "
            "version INTEGER NOT NULL DEFAULT 1)"
        )
        try:  # migrate an older table: add the optimistic-concurrency version column once
            conn.execute("ALTER TABLE context ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass  # column already present
        _paths.secure_sqlite_sidecars(p)
        return conn
    except Exception as exc:  # noqa: BLE001
        _note_error(exc)
        conn.close()
        return None


class _Backend(Protocol):
    """The storage seam the public functions shim over."""

    def mode(self) -> str:
        """``"local"`` (plaintext rows) or ``"bus"`` (rows must be sealed)."""
        ...

    def put(self, key: str, stored_value: str, meta: dict,
            expected_version: int | None = None) -> bool: ...

    def row(self, key: str) -> tuple[str, float, str] | None: ...

    def row_versioned(self, key: str) -> tuple[str, int] | None: ...

    def delete(self, key: str) -> bool: ...

    def entries(self) -> list[dict]: ...

    def location(self) -> str: ...


class _LocalSqliteBackend:
    """The original on-disk SQLite store, unchanged in behavior. Rows are stored
    as plaintext (local mode is not encrypted — the filesystem boundary is the
    local threat model); the codec lives above this backend, in the shim."""

    def mode(self) -> str:
        return "local"

    def put(self, key: str, value: str, meta: dict, expected_version: int | None = None) -> bool:
        """Store ``value`` under ``key``. Returns True on success.

        ``expected_version`` is optimistic concurrency: ``None`` overwrites
        unconditionally (bumping the version so CAS callers can still detect a later
        concurrent write); ``<= 0`` requires the key to be ABSENT (a fresh write);
        ``> 0`` requires the stored version to still match — otherwise the write is a
        no-op and this returns False (a concurrent writer got there first)."""
        key = (key or "").strip()
        if not key:
            return False
        description = str(meta.get("description") or "")
        conn = _connect()
        if conn is None:
            return False
        try:
            now = time.time()
            args = (key, value or "", now, (description or "").strip())
            if expected_version is None:
                conn.execute(
                    "INSERT INTO context (key, value, ts, description, version) "
                    "VALUES (?, ?, ?, ?, 1) ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, ts=excluded.ts, description=excluded.description, "
                    "version=version+1",
                    args,
                )
                ok = True
            elif expected_version <= 0:
                cur = conn.execute(
                    "INSERT INTO context (key, value, ts, description, version) "
                    "VALUES (?, ?, ?, ?, 1) ON CONFLICT(key) DO NOTHING",
                    args,
                )
                ok = cur.rowcount == 1
            else:
                cur = conn.execute(
                    "UPDATE context SET value=?, ts=?, description=?, version=version+1 "
                    "WHERE key=? AND version=?",
                    (value or "", now, (description or "").strip(), key, expected_version),
                )
                ok = cur.rowcount == 1
            conn.commit()
            if ok:
                global _sweep_counter
                _sweep_counter += 1
                if _sweep_counter % 100 == 0:
                    _sweep(conn)
            return ok
        except Exception as exc:  # noqa: BLE001
            _note_error(exc)  # connect succeeded but the write failed (locked/full DB) —
            print(f"ask_fable: context write failed: {exc}", file=sys.stderr)  # else invisible
            return False
        finally:
            conn.close()

    def row(self, key: str) -> tuple[str, float, str] | None:
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

    def row_versioned(self, key: str) -> tuple[str, int] | None:
        """(stored_value, version) for CAS callers, or None when absent."""
        key = (key or "").strip()
        if not key:
            return None
        conn = _connect()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT value, version FROM context WHERE key = ?", (key,)
            ).fetchone()
            return (row[0] or "", int(row[1] or 0)) if row else None
        except Exception as exc:  # noqa: BLE001
            _note_error(exc)
            return None
        finally:
            conn.close()

    def delete(self, key: str) -> bool:
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

    def entries(self) -> list[dict]:
        """Row metadata newest first, for the shim to decode. Best-effort → [].

        Values are never loaded wholesale (a store of big packs made a listing
        allocate the whole store): ``bytes`` comes from SQL ``length()``, which
        counts characters like ``len()`` (up to an embedded NUL). Only a value
        carrying the ``afctx1:`` armor prefix is included, because its size and
        description are sealed inside it for the shim to open."""
        conn = _connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT key, ts, description, length(value), "
                "CASE WHEN substr(value, 1, ?) = ? THEN value END "
                "FROM context ORDER BY ts DESC",
                (len(context_crypto.MAGIC), context_crypto.MAGIC),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            _note_error(exc)
            return []
        finally:
            conn.close()
        out: list[dict] = []
        for key, ts, description, size, armored in rows:
            row = {"key": key, "ts": float(ts or 0.0), "description": description or "",
                   "bytes": int(size or 0)}
            if armored is not None:
                row["value"] = armored
            out.append(row)
        return out

    def location(self) -> str:
        return str(_db_path())


_BACKEND: _Backend | None = None


def _backend() -> _Backend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = (
            context_bus.RemoteBusBackend()
            if context_bus.bus_url()
            else _LocalSqliteBackend()
        )
    return _BACKEND


def _reset_backend() -> None:
    """Drop the cached backend (tests change the bus env between cases)."""
    global _BACKEND
    _BACKEND = None


def _readable(stored: str, key: str) -> tuple[str | None, str | None, object | None]:
    """Decode one stored value.

    Local mode: plain values pass through; a sealed value is unsealed with the
    keyring (rollback guard off — the local file is the source of truth).

    Bus mode: the owner is untrusted, so the value MUST be a sealed envelope (an
    unsealed value would bypass the timestamp guard) and, once opened, its
    authenticated timestamp is checked against the local rollback high-water mark.

    Returns (value, sealed_description_or_None, error_or_None). The second item is
    None for plaintext rows (their description lives in the row) and the sealed
    description (possibly "") for envelopes."""
    bus = _backend().mode() == "bus"
    if not context_crypto.is_sealed(stored):
        if bus:
            return None, None, RuntimeError(
                f"context read at '{key}': the owner served an unsealed value on the bus"
            )
        return stored, None, None
    try:
        value, description = context_crypto.unseal(
            stored, key_name=key, keyring=context_crypto.load_keyring()
        )
    except context_crypto.CryptoError as exc:
        return None, None, RuntimeError(f"sealed context at '{key}': {exc}")
    if bus:
        try:
            _ver, _kid, _writer, head_ts = context_crypto.envelope_meta(stored)
            context_hwm.check(key, head_ts)
        except context_hwm.HwmError as exc:
            return None, None, RuntimeError(f"context read at '{key}': {exc}")
    return value, description, None


def location() -> str:
    """Resolved store location for diagnostics: the local DB path in local mode,
    the bus URL when the LAN bus is configured."""
    return _backend().location()


def db_path() -> str:
    """Back-compat alias for ``location()``."""
    return location()


def put(key: str, value: str, description: str = "", expected_version: int | None = None) -> bool:
    """Store (or overwrite) ``value`` under ``key``. Returns True on success.

    ``expected_version`` (from a prior ``get_versioned``) is optimistic concurrency on the
    LOCAL backend: the write becomes a no-op returning False when a concurrent writer has
    changed the row since it was read — so the caller learns its write did not land instead
    of silently clobbering. The bus backend keeps its own newest-wins control and ignores it.

    In bus mode the value is sealed here (client-side) before it leaves the
    process; the daemon stores only the armor. Sealing failure or any transport
    failure degrades the store — it never writes plaintext to the bus."""
    # Every public op starts clean: last_error() describes THIS op, so one transient
    # failure can't leave the store looking degraded to every later call.
    _clear_error()
    key = (key or "").strip()
    if not key:
        return False
    backend = _backend()
    meta: dict = {"description": description}
    stored = value or ""
    if backend.mode() == "bus":
        try:
            ts_ns = time.time_ns()
            writer = context_bus.writer_id()
            stored = context_crypto.seal(
                value or "", description or "", key_name=key, writer=writer, ts_ns=ts_ns
            )
            meta.update({"writer": writer, "ts_ns": ts_ns, "plaintext_bytes": len(value or "")})
        except context_crypto.CryptoError as exc:
            _map_failure(RuntimeError(f"seal failed for '{key}': {exc}"))
            return False
    try:
        ok = backend.put(key, stored, meta, expected_version)
    except context_bus.BusError as exc:
        _map_failure(exc)
        return False
    if ok and backend.mode() == "bus" and meta.get("ts_ns"):
        # Advance the local rollback mark immediately: the newly written ts is
        # now the newest version this client has produced. Best-effort by design.
        context_hwm.record(key, int(meta["ts_ns"]))
    return ok


def get(key: str) -> str | None:
    """Return the stored value for ``key``, or None if absent. A sealed value the
    keyring cannot open (or a transport failure) degrades the store
    (``last_error``) instead of returning."""
    _clear_error()
    key = (key or "").strip()
    try:
        row = _backend().row(key)
    except context_bus.BusError as exc:
        _map_failure(exc)
        return None
    if row is None:
        return None
    value, _desc, err = _readable(row[0], key)
    if err is not None:
        _map_failure(err)
        return None
    return value


def get_meta(key: str) -> tuple[str, float, str] | None:
    """Return (value, ts, description) for ``key``, or None if absent."""
    _clear_error()
    key = (key or "").strip()
    try:
        row = _backend().row(key)
    except context_bus.BusError as exc:
        _map_failure(exc)
        return None
    if row is None:
        return None
    value, sealed_desc, err = _readable(row[0], key)
    if err is not None:
        _map_failure(err)
        return None
    description = sealed_desc if sealed_desc is not None else row[2]
    return value or "", row[1], description


def get_versioned(key: str) -> tuple[str, int] | None:
    """Return (value, version) for optimistic-concurrency callers, or None if absent. The
    version is the local backend's row version; it is 0 on the bus backend (which has its own
    newest-wins control, not a row version). None on a degraded/unreadable read (``last_error``)."""
    _clear_error()
    key = (key or "").strip()
    try:
        row = _backend().row_versioned(key)
    except context_bus.BusError as exc:
        _map_failure(exc)
        return None
    if row is None:
        return None
    value, _desc, err = _readable(row[0], key)
    if err is not None:
        _map_failure(err)
        return None
    return (value or "", int(row[1]))


def _row(key: str) -> tuple[str, float, str] | None:
    """Back-compat alias for ``get_meta`` (kept for tests/callers)."""
    return get_meta(key)


def delete(key: str) -> bool:
    """Delete ``key``. Returns True if a row was removed."""
    _clear_error()
    key = (key or "").strip()
    if not key:
        return False
    backend = _backend()
    try:
        deleted = backend.delete(key)
    except context_bus.BusError as exc:
        _map_failure(exc)
        return False
    if deleted and backend.mode() == "bus":
        context_hwm.clear(key)  # this client removed the key; its mark is spent
    return deleted


def entries() -> list[dict]:
    """List stored keys with size/age/description (never the full value) so an
    agent can discover what the bus holds. Newest first. Best-effort → [].

    Backends list metadata, not blobs: a row carries its stored ``value`` only
    when that must be opened here for a sealed description (and, on the bus, only
    while small). A sealed row that cannot be opened is reported with an
    ``unreadable`` marker rather than failing the whole listing; a sealed bus row
    too large to open in a listing is reported with ``description_omitted``
    (``get`` the key for its description)."""
    _clear_error()
    try:
        rows = _backend().entries()
    except context_bus.BusError as exc:
        _map_failure(exc)
        return []
    now = time.time()
    out: list[dict] = []
    for r in rows:
        key = r.get("key") or ""
        ts = float(r.get("ts") or 0.0)
        entry = {
            "key": key, "bytes": int(r.get("bytes") or 0),
            "age_s": max(0, int(now - (ts or now))),
            "description": r.get("description") or "",
        }
        raw = r.get("value")
        if raw is not None:
            value, sealed_desc, err = _readable(raw, key)
            if err is not None:
                entry.update(bytes=len(raw), unreadable=str(err))
            else:
                entry["bytes"] = len(value or "")
                if sealed_desc is not None:
                    entry["description"] = sealed_desc
        elif r.get("sealed"):
            entry["description_omitted"] = True
        out.append(entry)
    return out
