"""Persistent per-(model, domain) calibration store — the foundation for calibrated councils.

``ask_falsify`` resolves claims into survived/killed outcomes; this durably aggregates them
per (model, domain) so a model's HISTORICAL accuracy in a domain can later weight
``ask_council``'s synthesis. Anchored to RESOLVED outcomes only — never peer agreement, which
is the conformity trap the design conference warned about — and keyed by a fine-grained domain,
because a coarse bucket (``rep[model,"reasoning"]``) is just a popularity contest.

Small, durable SQLite (WAL), mirroring :mod:`context_store`'s connection discipline and its
best-effort contract: the store never raises and never breaks a tool call. ``score`` uses
hierarchical shrinkage toward a prior so a model with 2 datapoints can't outrank one with 200.

This module only RECORDS and READS; wiring the score into ``ask_council`` weighting is a
separate, review-gated slice.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from pathlib import Path

from . import _paths, falsify_ledger

_SHRINK_K = 10   # datapoints at which the observed rate gets equal weight with the prior
_PRIOR = 0.5


def _db_path() -> Path:
    override = os.environ.get("ASK_FABLE_REPUTATION_PATH")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_state_dir() / "ask_fable" / "reputation.db"


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
    except Exception:  # noqa: BLE001 — the store must never break a tool call
        return None
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS outcomes "
            "(session TEXT NOT NULL, claim_key TEXT NOT NULL, model TEXT NOT NULL, "
            "domain TEXT NOT NULL, correct INTEGER NOT NULL, ts REAL, "
            "PRIMARY KEY (session, claim_key))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_outcomes_md ON outcomes(model, domain)")
        _paths.secure_sqlite_sidecars(p)
        return conn
    except Exception:  # noqa: BLE001
        conn.close()
        return None


def _shrunk(n: int, wins: int, k: int = _SHRINK_K, prior: float = _PRIOR) -> float:
    """Accuracy in [0,1], shrunk toward ``prior``: prior + n/(n+k)·(wins/n − prior)."""
    if n <= 0:
        return prior
    obs = wins / n
    return prior + (n / (n + k)) * (obs - prior)


def _outcome_key(claim_id: str, claim_text: str) -> str:
    """A per-run-stable idempotency key. Claim ids are MODEL-assigned (``H1``, ``C1`` …) and so
    are NOT unique across concurrent runs on one session; binding the id to a hash of the claim
    text means two different claims that happen to share an id no longer collide."""
    h = hashlib.sha256((claim_text or "").encode("utf-8")).hexdigest()[:16]
    return f"{(claim_id or '').strip()}:{h}"


def _insert(session: str, claim_key: str, model: str, domain: str, correct: bool) -> bool:
    """Upsert one outcome row: a repeat ``(session, claim_key)`` with the SAME outcome is a
    no-op, while a flipped one (a survived claim later killed by a contra, or a killed claim
    reopened that then survived) replaces it — one datapoint per claim, always its latest
    resolution, never double-counted even if the ledger save later fails. Returns True only
    when a row was written or changed. Best-effort, never raises."""
    model, domain = (model or "").strip(), (domain or "general").strip()
    if not model:
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        cur = conn.execute(
            "INSERT INTO outcomes (session, claim_key, model, domain, correct, ts) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session, claim_key) DO UPDATE SET "
            "correct = excluded.correct, ts = excluded.ts "
            "WHERE outcomes.correct <> excluded.correct",
            (session, claim_key, model, domain, int(bool(correct)), time.time()),
        )
        conn.commit()
        return cur.rowcount == 1
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


def record(model: str, domain: str, correct: bool) -> bool:
    """Append one anonymous calibration datapoint (a fresh unique key, always recorded) — for
    direct/manual use. The ledger feed uses ``record_outcome`` for idempotency."""
    return _insert("", uuid.uuid4().hex, model, domain, correct)


def record_outcome(session: str, claim_id: str, claim_text: str, model: str, domain: str,
                   correct: bool) -> bool:
    """Record one resolved claim's outcome under a durable ``(session, claim)`` idempotency
    key. Returns True on a new datapoint or a flipped outcome, False on a repeat."""
    session = (session or "").strip()
    if not session:
        return False
    return _insert(session, _outcome_key(claim_id, claim_text), model, domain, correct)


def _row(model: str, domain: str) -> tuple[int, int] | None:
    conn = _connect()
    if conn is None:
        return None
    try:
        r = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(correct), 0) FROM outcomes "
            "WHERE model = ? AND domain = ?",
            ((model or "").strip(), (domain or "general").strip()),
        ).fetchone()
        n = int(r[0]) if r else 0
        return (n, int(r[1])) if n else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def score(model: str, domain: str, *, k: int = _SHRINK_K, prior: float = _PRIOR) -> float:
    """Shrunk historical accuracy for ``(model, domain)``. Returns the prior for an unseen pair
    so a newcomer neither helps nor hurts until it has a track record."""
    row = _row(model, domain)
    return _shrunk(row[0], row[1], k, prior) if row else prior


def snapshot() -> list[dict]:
    """All rows with their shrunk score, most-observed first — for observability."""
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT model, domain, COUNT(*), COALESCE(SUM(correct), 0), MAX(ts) "
            "FROM outcomes GROUP BY model, domain ORDER BY COUNT(*) DESC"
        ).fetchall()
        return [
            {"model": r[0], "domain": r[1], "n": int(r[2]), "wins": int(r[3]),
             "score": round(_shrunk(int(r[2]), int(r[3])), 4), "updated": r[4]}
            for r in rows
        ]
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()


def record_ledger_outcomes(ledger: dict) -> int:
    """Feed a resolved ask_falsify ledger into the store: each survived/killed claim is one
    calibration datapoint for its author (survived = correct). Idempotent via a durable
    ``(session, claim)`` key in the DB — advancing OR replaying a persisted ledger never
    double-counts, and this no longer mutates the ledger (no ``_calibrated`` mark to keep in
    sync with the blob store). A claim whose status later flips updates its one datapoint.
    Returns how many outcomes were recorded or changed."""
    session = str((ledger or {}).get("session") or "").strip()
    recorded = 0
    for c in falsify_ledger.claims(ledger):
        if c.get("status") not in ("survived", "killed"):
            continue
        author = str(c.get("author") or "").strip()
        if author and record_outcome(
            session, str(c.get("id") or ""), str(c.get("claim") or ""),
            author, str(c.get("domain") or "general"), c.get("status") == "survived",
        ):
            recorded += 1
    return recorded
