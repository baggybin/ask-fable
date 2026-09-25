"""Mini-bus context store + its MCP handlers + context_ref resolution on `ask`."""

from __future__ import annotations

import asyncio
import base64
import os
import sqlite3
import time

import pytest

import ask_fable.server as server
from ask_fable import context_crypto, context_store
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CONTEXT_PATH", str(tmp_path / "context.db"))
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE", "0")  # keep the answer cache out of these tests
    monkeypatch.setenv("ASK_FABLE_SAVE", "0")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    # The backend dispatch is cached module-globally; isolate bus/keyring env and
    # rebuild it per test so no case leaks its bus configuration into the next.
    monkeypatch.delenv("ASK_FABLE_CONTEXT_BUS", raising=False)
    monkeypatch.delenv("ASK_FABLE_CONTEXT_KEYRING", raising=False)
    context_store._reset_backend()
    yield
    context_store._reset_backend()


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


def test_optimistic_cas_detects_a_concurrent_write():
    # F2b: get_versioned + put(expected_version=…) let a caller detect that a concurrent
    # writer changed the row since it read it, instead of silently clobbering.
    assert context_store.put("k", "v1") is True
    row = context_store.get_versioned("k")
    assert row is not None and row[0] == "v1"
    ver = row[1]
    assert context_store.put("k", "v2", expected_version=ver) is True   # matches → lands
    assert context_store.put("k", "v3", expected_version=ver) is False  # stale → refused
    assert context_store.get("k") == "v2"                               # not clobbered
    # a fresh-key CAS: expected_version 0 requires the key to be absent
    assert context_store.put("new", "x", expected_version=0) is True    # absent → inserted
    assert context_store.put("new", "y", expected_version=0) is False   # now present → refused
    assert context_store.get("new") == "x"


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


def test_entries_does_not_load_the_values():
    # The listing SELECTed every value, so a store of big packs allocated the whole
    # store just to list its keys.
    import tracemalloc

    for i in range(4):
        assert context_store.put(f"pack{i}", "x" * 2_000_000, f"pack {i}")
    tracemalloc.start()
    try:
        ents = context_store.entries()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert {e["key"]: e["bytes"] for e in ents} == {f"pack{i}": 2_000_000 for i in range(4)}
    assert all(e["description"] == f"pack {e['key'][-1]}" for e in ents)
    assert peak < 1_000_000  # not even one value was materialized


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
    eff, resolved, missing, degraded = server._resolve_context("INLINE_BIT", "repo:auth")
    assert "STORED_AUTH_CODE" in eff and "INLINE_BIT" in eff
    assert eff.index("STORED_AUTH_CODE") < eff.index("INLINE_BIT")  # stored first
    assert resolved == ["repo:auth"] and missing == [] and degraded == []


def test_resolve_reports_missing_key():
    eff, resolved, missing, degraded = server._resolve_context("", ["nope", "also_nope"])
    assert resolved == [] and missing == ["nope", "also_nope"] and eff == ""
    assert degraded == []  # a healthy store: genuinely absent, not degraded


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


def test_row_cap_evicts_oldest_when_set(monkeypatch, tmp_path):
    """M4: an operator-set ASK_FABLE_CONTEXT_MAX_ROWS bounds the store (default is
    unlimited, preserving the 'explicit artifact' design). Sweep fires at the 100th
    write and trims to 90% of the cap, evicting the oldest."""
    import ask_fable.context_store as cs
    monkeypatch.setenv("ASK_FABLE_CONTEXT_PATH", str(tmp_path / "context.db"))
    monkeypatch.setenv("ASK_FABLE_CONTEXT_MAX_ROWS", "10")
    cs._sweep_counter = 0
    for i in range(100):
        cs.put(f"k{i:03d}", f"v{i}")
    rows = cs.entries()
    assert len(rows) <= 10
    keys = {r["key"] for r in rows}
    assert "k099" in keys and "k000" not in keys  # newest kept, oldest evicted


# --- LAN codec seam: sealed rows, rollback guard, bus fail-closed ------------

def _make_keyring(tmp_path, psk=b"\x07" * 32, kid=1):
    p = tmp_path / "context_keyring"
    p.write_text(f"{kid}:{base64.b64encode(psk).decode()}\n")
    os.chmod(p, 0o600)
    return p


def _seal_into_db(key: str, value: str, description: str = "") -> None:
    """Write an ``afctx1:`` envelope straight into the local DB, the way a
    migrated or misconfigured fleet would leave one. Uses the keyring env set by
    the caller."""
    armored = context_crypto.seal(
        value, description, key_name=key, writer="hostA",
        ts_ns=1_760_000_000_000_000_000, keyring=context_crypto.load_keyring(),
    )
    context_store.put(key, "seed")  # ensure the table/row exists
    conn = sqlite3.connect(str(context_store._db_path()))
    conn.execute(
        "UPDATE context SET value=?, ts=?, description=? WHERE key=?",
        (armored, time.time(), "", key),
    )
    conn.commit()
    conn.close()


def test_sealed_row_without_keyring_is_degraded(tmp_path, monkeypatch):
    path = _make_keyring(tmp_path)
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(path))
    _seal_into_db("sealed", "PLAINTEXT_MARKER", "sealed desc")
    monkeypatch.delenv("ASK_FABLE_CONTEXT_KEYRING")
    context_store._clear_error()
    assert context_store.get("sealed") is None  # never the raw armor
    err = context_store.last_error()
    assert err is not None and "sealed" in err  # degraded, not "missing"
    assert context_store.get_meta("sealed") is None
    assert context_store.last_error() is not None


def test_sealed_row_with_keyring_unseals(tmp_path, monkeypatch):
    path = _make_keyring(tmp_path)
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(path))
    _seal_into_db("sealed", "PLAINTEXT_MARKER", "sealed desc")
    assert context_store.get("sealed") == "PLAINTEXT_MARKER"
    meta = context_store.get_meta("sealed")
    assert meta is not None
    assert meta[0] == "PLAINTEXT_MARKER" and meta[2] == "sealed desc"
    ents = {e["key"]: e for e in context_store.entries()}
    assert ents["sealed"]["bytes"] == len("PLAINTEXT_MARKER")
    assert ents["sealed"]["description"] == "sealed desc"
    assert "unreadable" not in ents["sealed"]


def test_entries_marks_unreadable_without_failing_list(tmp_path, monkeypatch):
    path = _make_keyring(tmp_path)
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(path))
    context_store.put("plain", "x" * 10, "plain row")
    _seal_into_db("sealed", "SECRET", "")
    monkeypatch.delenv("ASK_FABLE_CONTEXT_KEYRING")
    ents = {e["key"]: e for e in context_store.entries()}
    assert set(ents) == {"plain", "sealed"}  # one bad row doesn't fail the list
    assert "unreadable" not in ents["plain"]
    assert "unreadable" in ents["sealed"]


def test_plain_value_quoting_the_magic_is_not_sealed():
    text = "the afctx1: armor format is documented here"
    context_store.put("doc", text)
    assert context_store.get("doc") == text


def test_bus_env_fails_closed_when_daemon_unreachable(tmp_path, monkeypatch):
    path = _make_keyring(tmp_path)
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(path))
    sock = tmp_path / "nope.sock"  # nothing is listening here
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS", f"unix://{sock}")
    context_store._reset_backend()
    context_store._clear_error()
    assert context_store.put("k", "v") is False
    assert "unreachable" in (context_store.last_error() or "")
    assert context_store.get("k") is None
    assert context_store.entries() == []
    assert context_store.location() == f"unix://{sock}"
    monkeypatch.delenv("ASK_FABLE_CONTEXT_BUS")
    context_store._reset_backend()
    assert context_store.put("k", "v") is True
    assert context_store.get("k") == "v"


def test_bus_put_without_keyring_refuses_to_send(tmp_path, monkeypatch):
    # No keyring -> sealing fails -> the plaintext value must NOT be sent anywhere.
    monkeypatch.setenv("ASK_FABLE_CONTEXT_BUS", "unix:///nope.sock")
    monkeypatch.setenv("ASK_FABLE_CONTEXT_KEYRING", str(tmp_path / "no-keyring"))
    context_store._reset_backend()
    context_store._clear_error()
    assert context_store.put("k", "v") is False
    assert "seal failed" in (context_store.last_error() or "")


def test_bus_backend_plaintext_row_is_refused(monkeypatch):
    # Defense-in-depth: if a backend ever hands the shim an unsealed value in bus
    # mode, the read degrades instead of returning owner-supplied plaintext.
    class PlainBus:
        def mode(self):
            return "bus"

        def row(self, key):
            return ("PLAINTEXT_FROM_OWNER", 1.0, "")

    monkeypatch.setattr(context_store, "_backend", lambda: PlainBus())
    context_store._clear_error()
    assert context_store.get("k") is None
    assert "unsealed value on the bus" in (context_store.last_error() or "")


def test_location_matches_db_path_in_local_mode():
    assert context_store.location() == context_store.db_path()


def test_public_api_goes_through_the_backend_seam(monkeypatch):
    calls: dict = {}

    class FakeBackend:
        def mode(self):
            return "local"

        def put(self, key, value, meta, expected_version=None):
            calls["put"] = (key, value, meta, expected_version)
            return True

        def row(self, key):
            calls["row"] = key
            return ("V", 1.0, "D")

        def row_versioned(self, key):
            calls["row_versioned"] = key
            return ("V", 1)

        def delete(self, key):
            calls["delete"] = key
            return True

        def entries(self):
            calls["entries"] = True
            return []

        def location(self):
            return "fake://store"

    monkeypatch.setattr(context_store, "_backend", lambda: FakeBackend())
    assert context_store.put("k", "v", "d") is True
    assert calls["put"] == ("k", "v", {"description": "d"}, None)
    assert context_store.get("k") == "V"
    assert context_store.get_meta("k") == ("V", 1.0, "D")
    assert context_store.delete("k") is True
    assert context_store.entries() == []
    assert context_store.location() == "fake://store"
