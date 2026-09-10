"""A tiny best-effort answer cache — spare weak agents' re-ask loops.

Local coding agents in an edit/verify loop tend to ask the *same* question
against the *same* code several times in a row. Each miss is a full model call
(or a whole council fan-out). This caches the JSON payload of a successful answer
keyed on ``hash(tool + models + normalized question + context)`` — so an exact
re-ask returns instantly with a ``cached: true`` flag and a duplicate nudge,
instead of paying for the model again.

Deliberately NOT applied to the multi-turn ``ask`` tool: its answer depends on
server-side session history, so caching it would either break follow-ups or serve
a stale turn. Only the single-shot tools (``ask_m3``/``ask_glm``/``ask_ollama``)
and the two councils use it.

Storage: a single SQLite file at ``$ASK_FABLE_CACHE_PATH`` or
``${XDG_STATE_HOME:-~/.local/state}/ask_fable/cache.db`` (0600). Everything is
best-effort — any sqlite/OS error degrades to "no cache", never raises. Disable
with ``ASK_FABLE_CACHE=0``; tune the freshness window with ``ASK_FABLE_CACHE_TTL``
(seconds, default 3600).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

from . import _paths

_DEFAULT_TTL = 3600  # 1h — long enough to cover an edit/verify loop, short enough to stay fresh
_DEFAULT_MAX_ROWS = 10000  # cap so a long-running server doesn't grow cache.db without bound
_sweep_counter = 0  # bumped on every put; triggers a sweep every ~100 writes


def _enabled() -> bool:
    return (os.environ.get("ASK_FABLE_CACHE") or "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def ttl() -> int:
    try:
        return int(os.environ.get("ASK_FABLE_CACHE_TTL") or _DEFAULT_TTL)
    except (TypeError, ValueError):
        return _DEFAULT_TTL


def _max_rows() -> int:
    try:
        return int(os.environ.get("ASK_FABLE_CACHE_MAX_ROWS") or _DEFAULT_MAX_ROWS)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ROWS


def _db_path() -> Path:
    override = os.environ.get("ASK_FABLE_CACHE_PATH")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_state_dir() / "ask_fable" / "cache.db"


def _connect() -> sqlite3.Connection | None:
    try:
        p = _db_path()
        # Ensure the parent dir is 0o700 AND the file is 0o600 at create time
        # (no world-readable window between create and chmod).
        if not _paths.ensure_dir_secure(p.parent):
            return None
        if not p.exists():
            try:
                fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            except FileExistsError:
                pass  # raced with another writer — fine
            except OSError:
                return None
        conn = sqlite3.connect(str(p), timeout=2.0)
        # WAL mode = crash-safe; synchronous=NORMAL pairs with WAL for durability
        # without the latency of FULL. Safe because the cache is best-effort.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        conn.execute("CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, payload TEXT, ts REAL)")
        _paths.secure_sqlite_sidecars(p)
        return conn
    except Exception:  # noqa: BLE001 — caching must never break a tool call
        return None


# Bump when the STORED payload shape or the logic that computes it changes, so old
# entries can't be served under new semantics. v2: coverage-aware council consensus
# (+ consensus_votes field) — pre-v2 councils cached a differently-computed "consensus"
# and lack the vote vector, so they must miss rather than serve a stale verdict.
# v3: effort joined the key — a "quick" atlas/grok answer must not be served for a
# "deep" request on the same question (and vice-versa).
_KEY_VERSION = 3


def key(tool: str, models: list[str], question: str, context: str,
        effort: str | None = None) -> str:
    """Stable cache key. Question is whitespace/case-normalized so trivial edits
    still hit; context is hashed exactly (the same question against different code
    MUST miss); effort is part of the key because it changes the answer's depth.
    ``_KEY_VERSION`` scopes every key so a payload/semantics change invalidates the
    whole cache instead of serving stale-shaped answers."""
    q = unicodedata.normalize("NFKC", " ".join((question or "").split()).lower())
    canon = json.dumps(
        {
            "v": _KEY_VERSION,
            "tool": tool,
            "models": sorted(str(m) for m in (models or [])),
            "q": q,
            "c": hashlib.sha256((context or "").encode("utf-8")).hexdigest(),
            "e": (effort or "").strip().lower(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def get(k: str) -> tuple[dict, int] | None:
    """Return (payload, age_seconds) for a fresh hit, else None."""
    if not _enabled():
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT payload, ts FROM answers WHERE key = ?", (k,)).fetchone()
        if not row:
            return None
        delta = time.time() - row[1]  # compare on the float so TTL=0 means "always stale"
        if delta > ttl():
            conn.execute("DELETE FROM answers WHERE key = ?", (k,))  # opportunistic prune
            conn.commit()
            return None
        return json.loads(row[0]), max(0, int(delta))
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def put(k: str, payload: dict) -> None:
    """Store a payload (best-effort; only meaningful for ``status == ok``).

    Every ~100 writes, runs a background sweep: deletes all TTL-expired rows, and
    if the row count exceeds ``ASK_FABLE_CACHE_MAX_ROWS``, evicts the oldest 10%."""
    global _sweep_counter
    if not _enabled():
        return
    conn = _connect()
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO answers (key, payload, ts) VALUES (?, ?, ?)",
            (k, json.dumps(payload), time.time()),
        )
        conn.commit()
        # Periodic sweep — bounded cost amortized across writes.
        _sweep_counter += 1
        if _sweep_counter % 100 == 0:
            _sweep(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"ask_fable: cache write failed: {exc}", file=sys.stderr)
    finally:
        conn.close()


def _sweep(conn: sqlite3.Connection) -> None:
    """Delete TTL-expired rows; if still over the row cap, evict the oldest 10%."""
    try:
        now = time.time()
        t = ttl()
        conn.execute("DELETE FROM answers WHERE ts < ?", (now - t,))
        cap = _max_rows()
        if cap > 0:
            count = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
            if count > cap:
                evict = count - int(cap * 0.9)  # trim to 90% of cap
                conn.execute(
                    "DELETE FROM answers WHERE key IN (SELECT key FROM answers ORDER BY ts ASC LIMIT ?)",
                    (evict,),
                )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
