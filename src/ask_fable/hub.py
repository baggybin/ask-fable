"""Cross-instance session hub — visibility-only mirror, never touches the ask path.

Each ask_fable stdio instance mirrors its session activity into a shared SQLite
file (``hub.db``) on every completed success turn — single-oracle ``ask`` /
``ask_m3`` / … as well as multi-oracle ``ask_council`` / ``ask_chain`` /
``ask_debate``. The ``session_list`` / ``session_peek`` / ``session_stats`` tools
read it back, giving the operator a unified dashboard of what every coding agent
(across opencode / Claude Code / salient windows) is asking the oracles — and
letting agents explicitly coordinate by seeing each other's work.

**Hard boundary: this module NEVER injects anything into the ask path.** The
oracles (Fable, MiniMax, …) see only what the calling agent explicitly passes
(``question`` + ``context``). Hub data is written post-hoc (after the oracle
answered) and read only by the three coordination tools. The
``ask`` / ``ask_council`` / ``ask_chain`` / ``ask_debate`` handlers never read hub
data into their prompts. This isolation is what prevents self-reinforcing reasoning
loops and automatic cross-session information leakage into oracle answers.

Sharing model: **visibility-only**. ``SessionStore`` stays per-process; this is a
read-only mirror. ``blocked_streak`` and ``trusted`` are not persisted because they
are load-bearing for the in-process loop terminator and authorization. An
``sdk_session_id`` may be mirrored as metadata, but it is never used to resume a
session, so cross-process session *resume* is out of scope for this phase.

Storage mirrors ``cache.py``: single SQLite file at ``$ASK_FABLE_HUB_PATH`` or
``${XDG_STATE_HOME:-~/.local/state}/ask_fable/hub.db`` (0600, WAL), opened
per-operation. Multi-process safe by construction (verified). Retention mirrors
``cache.py`` too — a total row cap with an oldest-first sweep. Disable with
``ASK_FABLE_HUB=0``.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

from . import _paths

_DEFAULT_MAX_ROWS = 10000  # cap on turn-history rows; oldest evicted first
_DEFAULT_STALE_SECONDS = 300  # no heartbeat in 5 min → "stale" (instance idle/dead)
_DEFAULT_PREVIEW_CHARS = 160  # last_question truncation in session_list
_DEFAULT_STATS_WINDOW_S = 86400  # session_stats default: last 24h of turns
_sweep_counter = 0  # bumped per write_turn; triggers a sweep every ~100 writes


def _enabled() -> bool:
    return (os.environ.get("ASK_FABLE_HUB") or "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _max_rows() -> int:
    try:
        return int(os.environ.get("ASK_FABLE_HUB_MAX_ROWS") or _DEFAULT_MAX_ROWS)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ROWS


def _stale_seconds() -> int:
    try:
        return int(os.environ.get("ASK_FABLE_HUB_STALE_SECONDS") or _DEFAULT_STALE_SECONDS)
    except (TypeError, ValueError):
        return _DEFAULT_STALE_SECONDS


def _preview_chars() -> int:
    try:
        return int(os.environ.get("ASK_FABLE_HUB_PREVIEW_CHARS") or _DEFAULT_PREVIEW_CHARS)
    except (TypeError, ValueError):
        return _DEFAULT_PREVIEW_CHARS


def _db_path() -> Path:
    override = os.environ.get("ASK_FABLE_HUB_PATH")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_state_dir() / "ask_fable" / "hub.db"


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
        # timeout=10.0 — higher than cache.py's 2.0 since the hub may see more
        # concurrent writers (one per stdio instance). WAL + per-op connect/close
        # makes multi-process sharing safe; the longer busy_timeout absorbs bursts.
        conn = sqlite3.connect(str(p), timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        conn.execute(
            "CREATE TABLE IF NOT EXISTS turns ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts REAL, agent_id TEXT, project TEXT, session_key TEXT, "
            "question TEXT, answer TEXT, oracle TEXT, status TEXT, "
            "sdk_session_id TEXT, duration_ms INTEGER)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_key, agent_id, ts)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_meta ("
            "session_key TEXT, agent_id TEXT, project TEXT, "
            "last_question TEXT, last_answer TEXT, last_oracle TEXT, "
            "last_status TEXT, sdk_session_id TEXT, last_heartbeat REAL, "
            "started REAL, turn_count INTEGER, "
            "PRIMARY KEY (session_key, agent_id))"
        )
        _paths.secure_sqlite_sidecars(p)
        return conn
    except Exception:  # noqa: BLE001 — hub must never break a tool call
        return None


def _truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def write_turn(
    *,
    agent_id: str,
    project: str,
    session_key: str,
    question: str,
    answer: str,
    oracle: str,
    status: str,
    sdk_session_id: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Mirror one completed turn into the hub. Best-effort — never raises.

    Appends to ``turns`` and upserts ``session_meta``. Called from the server's
    success path AFTER the oracle answered, so this can never influence the answer.
    """
    if not _enabled():
        return
    conn = _connect()
    if conn is None:
        return
    now = time.time()
    try:
        conn.execute(
            "INSERT INTO turns (ts, agent_id, project, session_key, question, answer, "
            "oracle, status, sdk_session_id, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, agent_id, project, session_key, question, answer, oracle, status,
             sdk_session_id, duration_ms),
        )
        conn.execute(
            "INSERT INTO session_meta (session_key, agent_id, project, last_question, "
            "last_answer, last_oracle, last_status, sdk_session_id, last_heartbeat, "
            "started, turn_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(session_key, agent_id) DO UPDATE SET "
            "project=excluded.project, last_question=excluded.last_question, "
            "last_answer=excluded.last_answer, last_oracle=excluded.last_oracle, "
            "last_status=excluded.last_status, sdk_session_id=excluded.sdk_session_id, "
            "last_heartbeat=excluded.last_heartbeat, "
            "turn_count=session_meta.turn_count + 1",
            (session_key, agent_id, project, question, answer, oracle, status,
             sdk_session_id, now, now),
        )
        conn.commit()
        global _sweep_counter
        _sweep_counter += 1
        if _sweep_counter % 100 == 0:
            _sweep(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"ask_fable: hub write failed: {exc}", file=sys.stderr)
    finally:
        conn.close()


def list_sessions(
    *,
    project: str | None = None,
    all_projects: bool = False,
    limit: int = 50,
    active_only: bool = True,
) -> dict:
    """Dashboard view: one row per (session_key, agent_id), freshest first.

    Each entry carries last_question (truncated), oracle, status, heartbeat age,
    staleness, and turn_count. Scoped to ``project`` unless ``all_projects``.

    By default ``active_only`` hides sessions whose last heartbeat is older than
    the stale threshold (``ASK_FABLE_HUB_STALE_SECONDS``, default 5 min) so the
    dashboard shows live fleet work, not archaeology. Pass ``active_only=False``
    to list retained history (still ordered by heartbeat, still limited).
    """
    if not _enabled():
        return {"status": "ok", "enabled": False, "sessions": [], "active_only": active_only}
    conn = _connect()
    if conn is None:
        return {"status": "error", "kind": "store_unavailable"}
    try:
        now = time.time()
        stale = _stale_seconds()
        prev = _preview_chars()
        clauses: list[str] = []
        params: list = []
        if not all_projects and project:
            clauses.append("project = ?")
            params.append(project)
        if active_only:
            # Fresh = heartbeat within the stale window (inclusive of the boundary).
            clauses.append("last_heartbeat > ?")
            params.append(now - stale)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT session_key, agent_id, project, last_question, last_oracle, "
            f"last_status, last_heartbeat, started, turn_count FROM session_meta "
            f"{where} ORDER BY last_heartbeat DESC LIMIT ?",
            (*params, max(1, limit)),
        ).fetchall()
        sessions = []
        for r in rows:
            (skey, aid, proj, q, orc, st, hb, started, count) = r
            age = int(now - hb) if hb else None
            sessions.append({
                "session_key": skey,
                "agent_id": aid,
                "project": proj,
                "last_question": _truncate(q, prev),
                "last_oracle": orc,
                "last_status": st,
                "last_heartbeat_age_s": age,
                "stale": bool(age is not None and age > stale),
                "started": started,
                "turn_count": count,
            })
        return {
            "status": "ok",
            "enabled": True,
            "active_only": active_only,
            "stale_seconds": stale,
            "sessions": sessions,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "kind": "store_unavailable", "detail": str(exc)}
    finally:
        conn.close()


def peek_session(*, session_key: str, agent_id: str | None = None) -> dict:
    """Full turn history for one session — every Q/A in chronological order.

    If ``agent_id`` is None, returns turns across ALL agents for that session_key
    (a shared session label used by multiple instances). Bounded by retention.
    """
    if not _enabled():
        return {"status": "ok", "enabled": False, "turns": []}
    conn = _connect()
    if conn is None:
        return {"status": "error", "kind": "store_unavailable"}
    try:
        if agent_id:
            rows = conn.execute(
                "SELECT ts, agent_id, project, question, answer, oracle, status, "
                "duration_ms FROM turns WHERE session_key = ? AND agent_id = ? "
                "ORDER BY ts ASC",
                (session_key, agent_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, agent_id, project, question, answer, oracle, status, "
                "duration_ms FROM turns WHERE session_key = ? ORDER BY ts ASC",
                (session_key,),
            ).fetchall()
        turns = []
        for r in rows:
            (ts, aid, proj, q, a, orc, st, dur) = r
            turns.append({
                "ts": ts, "agent_id": aid, "project": proj,
                "question": q, "answer": a, "oracle": orc, "status": st,
                "duration_ms": dur,
            })
        return {"status": "ok", "enabled": True, "session_key": session_key, "turns": turns}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "kind": "store_unavailable", "detail": str(exc)}
    finally:
        conn.close()


def hub_stats(
    *,
    project: str | None = None,
    all_projects: bool = False,
    window_s: int | None = _DEFAULT_STATS_WINDOW_S,
) -> dict:
    """Aggregated oracle usage across all instances on this machine.

    Turn counts (``total_turns``, ``by_status``, ``by_oracle``, ``by_agent``)
    default to the last 24h (``window_s=86400``). Pass ``window_s=0`` for all
    retained history. Session counts are always full-scope for the project filter:
    ``total_sessions`` is every ``session_meta`` row; ``fresh_sessions`` is those
    with a heartbeat inside the stale window. Cross-instance — unlike the
    per-instance ``stats`` tool which only sees its own audit log.
    """
    if not _enabled():
        return {"status": "ok", "enabled": False}
    conn = _connect()
    if conn is None:
        return {"status": "error", "kind": "store_unavailable"}
    try:
        now = time.time()
        stale = _stale_seconds()
        # Normalize window: None → default 24h; 0 → no time filter; negative → default.
        if window_s is None:
            window_s = _DEFAULT_STATS_WINDOW_S
        try:
            window_s = int(window_s)
        except (TypeError, ValueError):
            window_s = _DEFAULT_STATS_WINDOW_S
        if window_s < 0:
            window_s = _DEFAULT_STATS_WINDOW_S

        turn_clauses: list[str] = []
        turn_params: list = []
        meta_clauses: list[str] = []
        meta_params: list = []
        if not all_projects and project:
            turn_clauses.append("project = ?")
            turn_params.append(project)
            meta_clauses.append("project = ?")
            meta_params.append(project)
        if window_s > 0:
            turn_clauses.append("ts >= ?")
            turn_params.append(now - window_s)
        turn_where = f"WHERE {' AND '.join(turn_clauses)}" if turn_clauses else ""
        meta_where = f"WHERE {' AND '.join(meta_clauses)}" if meta_clauses else ""

        total = conn.execute(
            f"SELECT COUNT(*) FROM turns {turn_where}", turn_params
        ).fetchone()[0]
        by_status = {}
        for st, n in conn.execute(
            f"SELECT status, COUNT(*) FROM turns {turn_where} GROUP BY status",
            turn_params,
        ):
            by_status[st] = n
        by_oracle = {}
        for orc, n in conn.execute(
            f"SELECT oracle, COUNT(*) FROM turns {turn_where} GROUP BY oracle",
            turn_params,
        ):
            by_oracle[orc] = n
        by_agent = {}
        for aid, n in conn.execute(
            f"SELECT agent_id, COUNT(*) FROM turns {turn_where} GROUP BY agent_id",
            turn_params,
        ):
            by_agent[aid] = n
        unknown_turns = int(by_agent.get("unknown") or 0)
        attributed_turns = int(total) - unknown_turns

        total_sessions = conn.execute(
            f"SELECT COUNT(*) FROM session_meta {meta_where}", meta_params
        ).fetchone()[0]
        fresh_clauses = list(meta_clauses) + ["last_heartbeat > ?"]
        fresh_params = list(meta_params) + [now - stale]
        fresh_where = f"WHERE {' AND '.join(fresh_clauses)}"
        fresh_sessions = conn.execute(
            f"SELECT COUNT(*) FROM session_meta {fresh_where}", fresh_params
        ).fetchone()[0]

        return {
            "status": "ok",
            "enabled": True,
            "window_s": window_s,
            "stale_seconds": stale,
            "total_turns": total,
            "attributed_turns": attributed_turns,
            "unknown_turns": unknown_turns,
            "total_sessions": total_sessions,
            "fresh_sessions": fresh_sessions,
            # Backward-compatible alias: "active" now means fresh (not all meta).
            "active_sessions": fresh_sessions,
            "by_status": by_status,
            "by_oracle": by_oracle,
            "by_agent": by_agent,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "kind": "store_unavailable", "detail": str(exc)}
    finally:
        conn.close()


def prune_junk(
    *,
    agent_id: str = "unknown",
    max_duration_ms: int = 1,
) -> dict:
    """Remove synthetic/unattributed turns that pollute the dashboard.

    Default target: ``agent_id='unknown'`` with ``duration_ms <= 1`` (or NULL) —
    the pattern left by test harnesses / handlers that mirrored without a real
    MCP client or wall-clock. Real oracle traffic is multi-second and attributed.

    Rebuilds ``session_meta`` for survivors (turn_count + last_* from remaining
    turns) and drops orphaned meta rows. Best-effort; never raises.
    """
    if not _enabled():
        return {"status": "ok", "enabled": False, "deleted_turns": 0}
    conn = _connect()
    if conn is None:
        return {"status": "error", "kind": "store_unavailable"}
    try:
        before = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        cur = conn.execute(
            "DELETE FROM turns WHERE agent_id = ? AND "
            "(duration_ms IS NULL OR duration_ms <= ?)",
            (agent_id, max(0, int(max_duration_ms))),
        )
        deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        # Drop meta rows that no longer have any turns. NOT EXISTS, not NOT IN:
        # one NULL session_key/agent_id in turns would make NOT IN match nothing.
        conn.execute(
            "DELETE FROM session_meta AS m WHERE NOT EXISTS "
            "(SELECT 1 FROM turns t WHERE t.session_key = m.session_key "
            "AND t.agent_id = m.agent_id)"
        )
        # Recount + refresh last_* for remaining meta from the latest surviving turn.
        rows = conn.execute(
            "SELECT session_key, agent_id FROM session_meta"
        ).fetchall()
        for skey, aid in rows:
            count = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE session_key = ? AND agent_id = ?",
                (skey, aid),
            ).fetchone()[0]
            latest = conn.execute(
                "SELECT question, answer, oracle, status, sdk_session_id, ts "
                "FROM turns WHERE session_key = ? AND agent_id = ? "
                "ORDER BY ts DESC LIMIT 1",
                (skey, aid),
            ).fetchone()
            if latest is None:
                continue
            q, a, orc, st, sdk, ts = latest
            conn.execute(
                "UPDATE session_meta SET turn_count = ?, last_question = ?, "
                "last_answer = ?, last_oracle = ?, last_status = ?, "
                "sdk_session_id = ?, last_heartbeat = ? "
                "WHERE session_key = ? AND agent_id = ?",
                (count, q, a, orc, st, sdk, ts, skey, aid),
            )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        return {
            "status": "ok",
            "enabled": True,
            "deleted_turns": deleted if deleted else max(0, before - after),
            "remaining_turns": after,
            "agent_id": agent_id,
            "max_duration_ms": max_duration_ms,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "kind": "store_unavailable", "detail": str(exc)}
    finally:
        conn.close()


def _sweep(conn: sqlite3.Connection) -> None:
    """Retention: if turn rows exceed the cap, evict the oldest first (to 90%)."""
    try:
        cap = _max_rows()
        if cap <= 0:
            return
        count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        if count > cap:
            evict = count - int(cap * 0.9)
            # Evict oldest turns, then drop orphaned session_meta rows.
            conn.execute(
                "DELETE FROM turns WHERE id IN (SELECT id FROM turns ORDER BY ts ASC LIMIT ?)",
                (evict,),
            )
            conn.execute(
                "DELETE FROM session_meta AS m WHERE NOT EXISTS "
                "(SELECT 1 FROM turns t WHERE t.session_key = m.session_key "
                "AND t.agent_id = m.agent_id)"
            )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
