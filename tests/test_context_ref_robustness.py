"""Bug hunt 2026-09-09 — context_ref must not silently drop context (C1 + E1).

Two independent ways the model used to answer blind:
  E1: a malformed context_ref (dict/int) slipped past _schema_error's anyOf gap
      and resolved to no keys.
  C1: a degraded store (locked/corrupt) made get() return None, indistinguishable
      from an absent key, so the call proceeded on whatever other context existed.
"""

from __future__ import annotations

import ask_fable.server as server

# --- E1: anyOf type validation ------------------------------------------------

def test_context_ref_rejects_non_string_or_array():
    assert server._schema_error("ask", {"question": "q", "context_ref": 123}) == (
        "invalid type for argument: context_ref"
    )
    assert server._schema_error("ask", {"question": "q", "context_ref": {"k": 1}}) == (
        "invalid type for argument: context_ref"
    )
    # bool must not sneak in as an int/str
    assert server._schema_error("ask", {"question": "q", "context_ref": True}) == (
        "invalid type for argument: context_ref"
    )


def test_context_ref_accepts_string_and_array():
    assert server._schema_error("ask", {"question": "q", "context_ref": "repo:auth"}) is None
    assert server._schema_error("ask", {"question": "q", "context_ref": ["a", "b"]}) is None


# --- C1: degraded store vs absent key -----------------------------------------

def _patch_store(monkeypatch, *, value, error):
    monkeypatch.setattr(server.context_store, "get", lambda k: value)
    monkeypatch.setattr(server.context_store, "last_error", lambda: error)
    monkeypatch.setattr(server.context_store, "db_path", lambda: "/tmp/context.db")


def test_degraded_store_hard_fails_even_with_other_context(monkeypatch):
    # get() returns None AND the store reports an error -> the blob may exist; do NOT
    # answer blind on the inline context the caller also passed.
    _patch_store(monkeypatch, value=None, error="database is locked")
    eff, resolved, missing, fail = server._prepare_context(
        {"question": "q", "context_ref": "repo:auth", "context": "a small inline note"}
    )
    assert fail is not None and fail["status"] == "store_degraded"
    assert fail["degraded_keys"] == ["repo:auth"]
    assert missing == []  # a degraded read is NOT a missing key


def test_absent_key_with_inline_context_still_proceeds(monkeypatch):
    # get() None but store is healthy -> genuinely absent; inline context remains, so
    # the call proceeds (missing reported, not fatal).
    _patch_store(monkeypatch, value=None, error=None)
    eff, resolved, missing, fail = server._prepare_context(
        {"question": "q", "context_ref": "typo:key", "context": "inline note"}
    )
    assert fail is None and missing == ["typo:key"] and "inline note" in eff


def test_absent_key_with_no_other_context_needs_context(monkeypatch):
    _patch_store(monkeypatch, value=None, error=None)
    _, _, _, fail = server._prepare_context({"question": "q", "context_ref": "typo:key"})
    assert fail is not None and fail["status"] == "needs_context"


def test_resolved_blob_is_included(monkeypatch):
    _patch_store(monkeypatch, value="BIG BLOB", error=None)
    eff, resolved, missing, fail = server._prepare_context(
        {"question": "q", "context_ref": "repo:auth"}
    )
    assert fail is None and resolved == ["repo:auth"] and "BIG BLOB" in eff
