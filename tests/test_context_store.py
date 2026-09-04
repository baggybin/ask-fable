"""Mini-bus context store + its MCP handlers + context_ref resolution on `ask`."""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.server as server
from ask_fable import context_store
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CONTEXT_PATH", str(tmp_path / "context.db"))
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE", "0")  # keep the answer cache out of these tests
    monkeypatch.setenv("ASK_FABLE_SAVE", "0")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


def _run(coro):
    return asyncio.run(coro)


# --- store primitives -------------------------------------------------------

def test_put_get_roundtrip():
    assert context_store.put("repo:auth", "def login(): ...", "auth module")
    assert context_store.get("repo:auth") == "def login(): ..."
    val, ts, desc = context_store.get_meta("repo:auth")
    assert val == "def login(): ..." and desc == "auth module" and ts > 0


def test_get_missing_is_none():
    assert context_store.get("nope") is None
    assert context_store.get_meta("nope") is None


# --- degraded-store disambiguation ------------------------------------------

def test_degraded_store_records_last_error(tmp_path, monkeypatch):
    # Parent path is a FILE, so the store can never open — get() still returns None,
    # but last_error() now distinguishes that from a genuinely absent key.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    monkeypatch.setenv("ASK_FABLE_CONTEXT_PATH", str(blocker / "context.db"))
    assert context_store.get("anything") is None
    assert context_store.last_error() is not None


def test_put_records_error_on_post_connect_write_failure(monkeypatch):
    # A clean connect followed by a failing write (locked/full DB) must still record
    # the error — otherwise context_write fails with no diagnostic, defeating the fix.
    import sqlite3

    class _FakeConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            ...

        def close(self):
            ...

    context_store._clear_error()
    monkeypatch.setattr(context_store, "_connect", lambda: _FakeConn())
    assert context_store.put("k", "v") is False
    err = context_store.last_error()
    assert err is not None and "locked" in err


def test_context_read_reports_degraded_store(monkeypatch):
    monkeypatch.setattr(context_store, "get_meta", lambda k: None)
    monkeypatch.setattr(context_store, "last_error", lambda: "OperationalError: disk I/O error")
    out = server._handle_context_read({"key": "repo:auth"})
    assert out["status"] == "error" and out["kind"] == "store_unavailable"
    assert "disk I/O error" in out["store_error"] and "db_path" in out


def test_context_read_absent_is_not_found_when_store_healthy(monkeypatch):
    monkeypatch.setattr(context_store, "get_meta", lambda k: None)
    monkeypatch.setattr(context_store, "last_error", lambda: None)
    out = server._handle_context_read({"key": "nope"})
    assert out["kind"] == "not_found" and "store_error" not in out


def test_context_list_flags_degraded_store(monkeypatch):
    monkeypatch.setattr(context_store, "entries", lambda: [])
    monkeypatch.setattr(context_store, "last_error", lambda: "OperationalError: database is locked")
    out = server._handle_context_list({})
    assert out["status"] == "ok" and "store_error" in out and "db_path" in out


def test_overwrite_and_delete():
    context_store.put("k", "v1")
    context_store.put("k", "v2")
    assert context_store.get("k") == "v2"
    assert context_store.delete("k") is True
    assert context_store.delete("k") is False  # already gone
    assert context_store.get("k") is None


def test_null_ts_row_does_not_raise(tmp_path):
    # An externally-written / corrupt DB with a NULL ts must not break the store's
    # "never raises" contract (entries/get_meta age math).
    import sqlite3
    db = tmp_path / "context.db"
    context_store.put("seed", "x")  # ensures the table exists
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT OR REPLACE INTO context (key, value, ts, description) VALUES (?,?,?,?)",
                 ("nullts", "v", None, None))
    conn.commit()
    conn.close()
    ents = {e["key"]: e for e in context_store.entries()}  # must not raise
    assert ents["nullts"]["age_s"] == 0
    val, ts, desc = context_store.get_meta("nullts")  # must not raise
    assert val == "v" and ts == 0.0 and desc == ""


def test_entries_lists_metadata_not_values():
    context_store.put("a", "x" * 10, "first")
    context_store.put("b", "y" * 20, "second")
    ents = {e["key"]: e for e in context_store.entries()}
    assert ents["a"]["bytes"] == 10 and ents["a"]["description"] == "first"
    assert ents["b"]["bytes"] == 20
    assert "value" not in ents["a"]  # never leak the full blob into a listing


# --- handlers ---------------------------------------------------------------

def test_write_read_handlers():
    out = server._handle_context_write({"key": "repo:x", "value": "code here", "description": "d"})
    assert out["status"] == "ok" and out["key"] == "repo:x" and out["bytes"] == 9
    got = server._handle_context_read({"key": "repo:x"})
    assert got["status"] == "ok" and got["value"] == "code here" and got["description"] == "d"


def test_read_not_found():
    out = server._handle_context_read({"key": "ghost"})
    assert out["status"] == "error" and out["kind"] == "not_found"


def test_write_rejects_empty():
    assert server._handle_context_write({"key": "", "value": "x"})["kind"] == "bad_args"
    assert server._handle_context_write({"key": "k", "value": "  "})["kind"] == "bad_args"


def test_list_and_delete_handlers():
    server._handle_context_write({"key": "k1", "value": "a"})
    server._handle_context_write({"key": "k2", "value": "b"})
    lst = server._handle_context_list({})
    assert lst["count"] == 2 and {e["key"] for e in lst["entries"]} == {"k1", "k2"}
    assert server._handle_context_delete({"key": "k1"})["deleted"] is True
    assert server._handle_context_list({})["count"] == 1


# --- resolution -------------------------------------------------------------

def test_resolve_merges_stored_and_inline():
    context_store.put("repo:auth", "STORED_AUTH_CODE")
    eff, resolved, missing = server._resolve_context("INLINE_BIT", "repo:auth")
    assert "STORED_AUTH_CODE" in eff and "INLINE_BIT" in eff
    assert eff.index("STORED_AUTH_CODE") < eff.index("INLINE_BIT")  # stored first
    assert resolved == ["repo:auth"] and missing == []


def test_resolve_reports_missing_key():
    eff, resolved, missing = server._resolve_context("", ["nope", "also_nope"])
    assert resolved == [] and missing == ["nope", "also_nope"] and eff == ""


def test_ask_pulls_context_ref(monkeypatch):
    context_store.put("repo:auth", "SECRET_MARKER_CODE")
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    seen = {}

    async def fake_run(question, context="", **kw):
        seen["context"] = context
        return OracleResult("ok", text="looks fine", session_id="sid")

    monkeypatch.setattr(server.fable, "run", fake_run)
    out = _run(server._handle_ask(
        server.SessionStore(),
        {"question": "is auth ok?", "context": "extra", "context_ref": "repo:auth"},
    ))
    assert "SECRET_MARKER_CODE" in seen["context"]  # the stored blob reached the model
    assert out["context_ref_resolved"] == ["repo:auth"]
    assert "context_ref_missing" not in out


# --- the missing-key decision table -----------------------------------------

def test_hard_fail_all_refs_missing_and_no_context(monkeypatch):
    context_store.put("guard-py", "code")  # a near-miss key for the did-you-mean
    called = {"n": 0}

    async def fake_run(*a, **k):
        called["n"] += 1
        return OracleResult("ok", text="should not run")

    monkeypatch.setattr(server.fable, "run", fake_run)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    out = _run(server._handle_ask(server.SessionStore(),
                                  {"question": "review it", "context_ref": "gaurd-py"}))
    assert out["status"] == "needs_context"  # all refs missing + no other context
    assert called["n"] == 0  # model never called
    mk = out["missing_keys"][0]
    assert mk["key"] == "gaurd-py" and "guard-py" in mk["did_you_mean"]


def test_missing_ref_but_inline_context_still_proceeds(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))

    async def fake_run(question, context="", **kw):
        return OracleResult("ok", text="ok", session_id="s")

    monkeypatch.setattr(server.fable, "run", fake_run)
    out = _run(server._handle_ask(server.SessionStore(),
                                  {"question": "q", "context": "inline code", "context_ref": "ghost"}))
    assert out["status"] == "ok"  # inline context remains → not fatal
    assert out["context_ref_missing"] == ["ghost"]


def test_prepare_context_history_prevents_hard_fail():
    # missing ref, no inline context, but session history present → proceed
    _, resolved, missing, fail = server._prepare_context({"context_ref": "ghost"}, has_history=True)
    assert missing == ["ghost"] and fail is None


def test_single_model_resolves_context_ref(monkeypatch):
    context_store.put("k", "MARKER")
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    seen = {}

    async def fake_run(question, context="", **kw):
        seen["context"] = context
        return OracleResult("ok", text="fine", model="MiniMax-M3")

    monkeypatch.setattr(server.minimax, "run", fake_run)
    out = _run(server._handle_m3({"question": "q", "context_ref": "k"}))
    assert "MARKER" in seen["context"] and out["context_ref_resolved"] == ["k"]


def test_council_resolves_and_reports_refs(monkeypatch):
    context_store.put("shared", "SHARED_BOOTSTRAP")
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    seen = {}

    async def fake_oracle_run(key, question, context=""):
        seen.setdefault("contexts", []).append(context)
        return OracleResult("ok", key=key, text="ans", model=key)

    async def fake_fable_run(question, context="", **kw):
        return OracleResult("ok", text="merged")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable_run)
    out = _run(server._handle_council(
        {"question": "big call", "context_ref": "shared", "models": ["fable", "minimax"]}
    ))
    assert out["status"] == "ok"
    assert out["context_ref_resolved"] == ["shared"]
    assert all("SHARED_BOOTSTRAP" in c for c in seen["contexts"])  # every panelist saw it
