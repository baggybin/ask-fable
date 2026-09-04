"""SessionStore: lock-protected mutations, atomic transcript dump."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import ask_fable.sessions as sessions


def test_bump_blocked_is_thread_safe():
    """1000 concurrent increments should never lose updates."""
    store = sessions.SessionStore()

    def hammer():
        for _ in range(1000):
            store.bump_blocked("k", True)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.bump_blocked("k", False) == 0  # reset still works
    # After 8 threads × 1000 = 8000 blocked increments, the counter must be exactly 8000.
    # (We re-bump to 8000 by reading the value; can't read directly without resetting.)
    # Easier: a fresh store + a single thread that records turn then bumps.
    store2 = sessions.SessionStore()

    def bump_n(n):
        for _ in range(n):
            store2.bump_blocked("k2", True)

    t1 = threading.Thread(target=bump_n, args=(5000,))
    t2 = threading.Thread(target=bump_n, args=(5000,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # 10000 increments total — the lock must have serialized them all.
    streak_after = store2.get("k2").blocked_streak
    assert streak_after == 10000, f"expected 10000 (no lost updates), got {streak_after}"


def test_record_turn_is_thread_safe():
    store = sessions.SessionStore()

    def record(n):
        for i in range(n):
            store.record_turn("k", question=f"q{i}", answer=f"a{i}", session_id="sid")

    threads = [threading.Thread(target=record, args=(100,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.get("k").turns) == 800


def test_reset_dumps_to_file_atomically(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = sessions.SessionStore()
    store.record_turn("my-topic", question="Q1", answer="A1", session_id=None, thinking="thoughts")
    store.record_turn("my-topic", question="Q2", answer="A2", session_id=None, thinking="")
    path = store.reset("my-topic", save=True)
    assert path is not None
    p = Path(path)
    assert p.exists()
    text = p.read_text()
    assert "Q1" in text and "A1" in text and "Q2" in text and "A2" in text
    # File mode 0o600.
    if sys.platform != "win32":
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
    # No leftover .tmp files.
    leftover = list(p.parent.glob(".my-topic-*.tmp"))
    assert leftover == []


def test_retention_prunes_oldest_session_dumps(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("ASK_FABLE_MAX_SESSIONS", "3")
    store = sessions.SessionStore()
    for i in range(5):
        store.record_turn(f"topic-{i}", question="q", answer="a", session_id=None)
        store.reset(f"topic-{i}", save=True)
    dumps = sorted((tmp_path / "ask_fable" / "sessions").glob("*.md"))
    assert len(dumps) == 3


def test_retention_never_deletes_foreign_markdown(monkeypatch, tmp_path):
    """The retention cap must only ever prune transcripts _dump() itself wrote."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("ASK_FABLE_MAX_SESSIONS", "1")
    d = tmp_path / "ask_fable" / "sessions"
    d.mkdir(parents=True)
    notes = d / "my-notes.md"
    notes.write_text("precious")
    os.utime(notes, (0, 0))  # oldest file in the dir by far
    store = sessions.SessionStore()
    for i in range(3):
        store.record_turn(f"t-{i}", question="q", answer="a", session_id=None)
        store.reset(f"t-{i}", save=True)
    assert notes.exists() and notes.read_text() == "precious"


def test_dumps_of_slug_colliding_keys_do_not_clobber(monkeypatch, tmp_path):
    # 'feat/auth' and 'feat:auth' both slugify to 'feat_auth'; both dumps must survive.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = sessions.SessionStore()
    store.record_turn("feat/auth", question="Q-slash", answer="A", session_id=None)
    store.record_turn("feat:auth", question="Q-colon", answer="A", session_id=None)
    p1 = store.reset("feat/auth", save=True)
    p2 = store.reset("feat:auth", save=True)
    assert p1 and p2 and p1 != p2
    assert Path(p1).exists() and Path(p2).exists()
    assert "Q-slash" in Path(p1).read_text() and "Q-colon" in Path(p2).read_text()


def test_reset_without_save_returns_none():
    store = sessions.SessionStore()
    store.record_turn("k", question="q", answer="a", session_id=None)
    assert store.reset("k", save=False) is None
    # The session is still gone.
    assert store.resume_id("k") is None


# ── trusted field ────────────────────────────────────────────────────────


def test_session_trusted_defaults_to_false():
    store = sessions.SessionStore()
    assert store.is_trusted("new-session") is False


def test_session_set_trusted():
    store = sessions.SessionStore()
    store.set_trusted("sec-research", True)
    assert store.is_trusted("sec-research") is True


def test_session_unset_trusted():
    store = sessions.SessionStore()
    store.set_trusted("sec-research", True)
    store.set_trusted("sec-research", False)
    assert store.is_trusted("sec-research") is False


def test_trusted_does_not_affect_other_sessions():
    store = sessions.SessionStore()
    store.set_trusted("sec-research", True)
    assert store.is_trusted("other-topic") is False


def test_trusted_field_persists_across_turns():
    store = sessions.SessionStore()
    store.set_trusted("sec-research", True)
    store.record_turn("sec-research", question="q1", answer="a1", session_id=None)
    assert store.is_trusted("sec-research") is True
