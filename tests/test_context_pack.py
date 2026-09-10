"""context_pack tool — the server handler over safe_fs + resolver + the bus.

End-to-end through the real context store (redirected to a tmp SQLite): the
not-configured gate, rejection propagation into `skipped`, the budget partial,
the untouched-store-on-empty rule, store-failure, and the happy-path roundtrip.
"""

from __future__ import annotations

import os

import pytest

import ask_fable.server as server
from ask_fable import context_store


@pytest.fixture
def packenv(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    return 1\n")
    (repo / "b.py").write_text("def b():\n    return 2\n")
    (repo / ".env").write_text("SECRET=1")
    monkeypatch.setenv("ASK_FABLE_PROJECT_ROOT", str(repo))
    monkeypatch.setenv("ASK_FABLE_CONFIG_FILE", str(tmp_path / "none.json"))  # no config shadow
    monkeypatch.setenv("ASK_FABLE_CONTEXT_PATH", str(tmp_path / "ctx.db"))    # tmp bus
    return os.path.realpath(str(repo))


# --- gates --------------------------------------------------------------------

def test_not_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("ASK_FABLE_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("ASK_FABLE_CONFIG_FILE", str(tmp_path / "none.json"))
    r = server._handle_context_pack({"paths": ["a.py"], "key": "k"})
    assert r["status"] == "error" and r["kind"] == "not_configured" and r["version"] == 1


def test_bad_args(packenv):
    assert server._handle_context_pack({"paths": ["a.py"]})["kind"] == "bad_args"  # no key
    assert server._handle_context_pack({"paths": [], "key": "k"})["kind"] == "bad_args"
    assert server._handle_context_pack({"paths": "a.py", "key": "k"})["kind"] == "bad_args"


# --- happy path + roundtrip ---------------------------------------------------

def test_happy_path_roundtrip(packenv):
    r = server._handle_context_pack({"paths": ["a.py", "b.py"], "key": "repo"})
    assert r["status"] == "ok" and r["complete"] is True and r["version"] == 1
    assert [f["path"] for f in r["files"]] == ["a.py", "b.py"]
    assert r["skipped"] == []
    # stored on the bus with a manifest head + deterministic description
    value, _, desc = context_store.get_meta("repo")
    assert value.startswith("[context_pack key='repo' files=2 skipped=0 complete=true]")
    assert "=== a.py ===" in value and "return 2" in value
    assert desc.startswith("context_pack:repo:")


def test_description_is_deterministic(packenv):
    a = server._handle_context_pack({"paths": ["a.py"], "key": "k1"})
    b = server._handle_context_pack({"paths": ["a.py"], "key": "k2"})
    _, _, da = context_store.get_meta("k1")
    _, _, db = context_store.get_meta("k2")
    # same specs -> same fingerprint suffix (idempotent re-pack), keyed part differs
    assert da.split(":")[-1] == db.split(":")[-1]
    assert a["status"] == b["status"] == "ok"


# --- rejection propagation ----------------------------------------------------

def test_escape_and_blocked_land_in_skipped_but_good_file_packs(packenv):
    r = server._handle_context_pack({"paths": ["../../etc/passwd", ".env", "a.py"], "key": "mix"})
    assert r["status"] == "ok" and r["complete"] is False
    reasons = {s["spec"]: s["reason"] for s in r["skipped"]}
    assert reasons["../../etc/passwd"] == "escape"
    assert reasons[".env"] == "blocked"
    assert [f["path"] for f in r["files"]] == ["a.py"]


def test_empty_pack_leaves_store_untouched(packenv):
    r = server._handle_context_pack({"paths": ["../../etc/passwd", ".env"], "key": "none"})
    assert r["status"] == "error" and r["kind"] == "empty_pack" and r["complete"] is False
    assert context_store.get("none") is None  # nothing written


def test_budget_partial(packenv):
    # a.py block ("=== a.py ===\n" + body) ~34 chars; cap admits one, refuses the other.
    r = server._handle_context_pack({"paths": ["a.py", "b.py"], "key": "cap", "max_chars": 40})
    assert r["status"] == "ok" and r["complete"] is False
    assert len(r["files"]) == 1
    assert any(s["reason"] == "budget_exhausted" for s in r["skipped"])


def test_store_failure(packenv, monkeypatch):
    monkeypatch.setattr(context_store, "put", lambda *a, **k: False)
    r = server._handle_context_pack({"paths": ["a.py"], "key": "repo"})
    assert r["status"] == "error" and r["kind"] == "store_failed"


# --- wiring -------------------------------------------------------------------

def test_schema_wired():
    assert server._CONTEXT_PACK_SCHEMA["required"] == ["paths", "key"]
    assert server._CONTEXT_PACK_SCHEMA["properties"]["paths"]["type"] == "array"
    # the description constant is imported into the server (list_tools registration)
    assert "Point, don't paste" in server.CONTEXT_PACK_TOOL_DESCRIPTION
