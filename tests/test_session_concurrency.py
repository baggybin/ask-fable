"""Bug hunt 2026-09-09 — R1: concurrent same-session `ask` calls must serialize."""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE", "0")
    monkeypatch.setenv("ASK_FABLE_SAVE", "0")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def test_same_session_calls_serialize_and_chain_resume(monkeypatch):
    seen_resume = []

    async def fake_run(question, context="", *, resume=None, on_think=None):
        seen_resume.append(resume)
        await asyncio.sleep(0.02)  # force overlap if the lock is absent
        return OracleResult("ok", text="ans", session_id=f"sid{len(seen_resume)}")

    monkeypatch.setattr(server.fable, "run", fake_run)
    store = server.SessionStore()

    async def drive():
        return await asyncio.gather(
            server._handle_ask(store, {"question": "q1", "session": "s"}),
            server._handle_ask(store, {"question": "q2", "session": "s"}),
        )

    asyncio.run(drive())
    # Serialized: the first call resumes from nothing; the second resumes from the
    # first's recorded session id. Without the lock both would see resume=None (fork).
    assert seen_resume == [None, "sid1"]


def test_different_sessions_do_not_serialize(monkeypatch):
    """Two DIFFERENT sessions must run concurrently (independent locks)."""
    order = []

    async def fake_run(question, context="", *, resume=None, on_think=None):
        order.append(("start", question))
        await asyncio.sleep(0.02)
        order.append(("end", question))
        return OracleResult("ok", text="ans", session_id="sid")

    monkeypatch.setattr(server.fable, "run", fake_run)
    store = server.SessionStore()

    async def drive():
        await asyncio.gather(
            server._handle_ask(store, {"question": "qa", "session": "a"}),
            server._handle_ask(store, {"question": "qb", "session": "b"}),
        )

    asyncio.run(drive())
    # Both start before either ends → interleaved, not serialized.
    assert order[0][0] == "start" and order[1][0] == "start"


def test_session_locks_do_not_accumulate(monkeypatch):
    """Verifier follow-up: the lock registry is a WeakValueDictionary, so a lock for
    an idle session is evicted once nothing holds it (a plain dict leaked one entry
    per distinct slug forever)."""
    import gc

    async def fake_run(question, context="", *, resume=None, on_think=None):
        return OracleResult("ok", text="ans", session_id="sid")

    monkeypatch.setattr(server.fable, "run", fake_run)

    async def drive():
        await server._handle_ask(server.SessionStore(), {"question": "q", "session": "ephemeral-xyz"})

    asyncio.run(drive())
    gc.collect()
    live_slugs = [slug for (_loop, slug) in server._SESSION_LOCKS.keys()]
    assert "ephemeral-xyz" not in live_slugs  # evicted once idle
