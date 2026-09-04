"""In-memory Fable conversation sessions + dump-on-reset.

A session keeps the resumable SDK ``session_id`` (so follow-ups continue Fable's
context server-side) plus a lightweight text transcript used only for the
dump-to-file feature. State lives in the server process and clears on restart.

"New topic → dump/store then clear": ``reset`` writes the transcript to a
per-session markdown file (0600) under the state dir when ``save`` is set, then
drops the session so the next ``ask`` starts fresh.

Concurrency: ``SessionStore`` carries a ``threading.Lock`` (not ``asyncio.Lock`` —
the mutating methods are sync, called between awaits) so concurrent ``ask`` calls
on the same session key can't interleave their ``record_turn`` / ``bump_blocked``
updates and lose increments (the PR2 loop terminator depends on ``blocked_streak``
being a faithful counter, not a last-writer-wins).
"""

from __future__ import annotations

import hashlib
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import _paths

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sessions_dir() -> Path:
    return _paths.xdg_state_dir() / "ask_fable" / "sessions"


def _max_files() -> int:
    """Max session transcript files to keep (0 = unlimited)."""
    return _paths.int_env("ASK_FABLE_MAX_SESSIONS", 0)


# Matches only names _dump() itself writes: {slug}-{stamp}-{khash}.md with a
# %Y%m%dT%H%M%S%fZ stamp and 6-hex key hash. The retention prune uses this so
# files ask-fable did not write are never deleted.
_OWNED_RE = re.compile(r"[A-Za-z0-9._-]+-\d{8}T\d{12}Z-[0-9a-f]{6}\.md")


@dataclass
class Session:
    key: str
    sdk_session_id: str | None = None
    turns: list[dict] = field(default_factory=list)  # {"q","a","ts"}
    started: float = field(default_factory=time.time)
    blocked_streak: int = 0  # consecutive needs_more_context turns (PR2 loop terminator)
    trusted: bool = False  # operator-authorized: guard runs log-only on this session


class SessionStore:
    """Process-lifetime map of session key → Session.

    All mutating methods are protected by a single ``threading.Lock``. Under
    cooperative-scheduling (the default for the MCP stdio transport) the
    asyncio event loop is itself the serializer, so the lock is belt-and-
    suspenders — it kicks in if a future transport ever becomes multi-threaded
    or multi-process. A ``threading.Lock`` is used (not ``asyncio.Lock``)
    because these methods are called from sync code between awaits, and
    ``asyncio.Lock`` requires its owning event loop to be running.

    The PR2 loop terminator depends on ``blocked_streak`` being a faithful
    counter, not a last-writer-wins value, so this lock is non-negotiable.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    @property
    def lock(self) -> threading.Lock:
        """The store's mutex — exposed for callers that want to bracket a
        multi-operation transaction (e.g. an external audit flush)."""
        return self._lock

    def get(self, key: str) -> Session:
        s = self._sessions.get(key)
        if s is None:
            s = Session(key=key)
            self._sessions[key] = s
        return s

    def resume_id(self, key: str) -> str | None:
        s = self._sessions.get(key)
        return s.sdk_session_id if s else None

    def bump_blocked(self, key: str, blocked: bool) -> int:
        """Track consecutive 'needs_more_context' turns on a session. Increments the
        streak when ``blocked``, resets it to 0 otherwise; returns the new streak.
        Drives the PR2 loop terminator so an agent can't re-ask forever.

        Holds the store lock for the read-modify-write so concurrent ``ask`` calls
        on the same key cannot interleave their increments and under-count."""
        with self._lock:
            s = self.get(key)
            s.blocked_streak = s.blocked_streak + 1 if blocked else 0
            return s.blocked_streak

    def set_trusted(self, key: str, trusted: bool) -> None:
        with self._lock:
            self.get(key).trusted = trusted

    def is_trusted(self, key: str) -> bool:
        s = self._sessions.get(key)
        return s.trusted if s else False

    def record_turn(
        self,
        key: str,
        question: str,
        answer: str,
        session_id: str | None,
        thinking: str = "",
    ) -> None:
        with self._lock:
            s = self.get(key)
            if session_id:
                s.sdk_session_id = session_id
            s.turns.append(
                {
                    "q": question,
                    "a": answer,
                    "think": thinking or "",
                    "ts": _paths.now_iso_z(),
                }
            )

    def reset(self, key: str, *, save: bool = True) -> str | None:
        """Drop the session. When ``save`` and it has turns, first write the
        transcript to a file and return its path (str); else return None."""
        with self._lock:
            s = self._sessions.pop(key, None)
        if s is None or not s.turns:
            return None
        if not save:
            return None
        return _dump(s)


def _dump(s: Session) -> str | None:
    try:
        d = _sessions_dir()
        slug = _SLUG_RE.sub("_", s.key).strip("_") or "session"
        # Microsecond stamp + short key hash: distinct keys can collapse to one slug
        # (feat/auth vs feat:auth), and write_secure REPLACES — a colliding name
        # would silently destroy the earlier transcript.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        khash = hashlib.sha256(s.key.encode("utf-8")).hexdigest()[:6]
        path = d / f"{slug}-{stamp}-{khash}.md"
        lines = [f"# ask_fable session: {s.key}", ""]
        for i, t in enumerate(s.turns, 1):
            lines += [f"## Turn {i} — {t['ts']}", "", "**Q:**", "", t["q"], ""]
            if t.get("think"):
                lines += ["**Thinking:**", "", t["think"], ""]
            lines += ["**A:**", "", t["a"], ""]
        if _paths.write_secure(path, "\n".join(lines)):
            keep = _max_files()
            if keep:
                _paths.prune_dir(d, keep=keep, pattern=_OWNED_RE)
            return str(path)
        return None
    except Exception as exc:  # noqa: BLE001 — dump must never break the tool
        print(f"ask_fable: session dump failed: {exc}", file=sys.stderr)
        return None
