"""ask_fable answer cache — key stability, TTL, enable/disable, and a handler hit."""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.cache as cache
import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


@pytest.fixture
def _cache_on(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CACHE", "1")  # conftest disables it by default


def _run(coro):
    return asyncio.run(coro)


def test_key_normalizes_question_but_not_context():
    # whitespace/case differences in the question collapse to the same key
    assert cache.key("ask_m3", ["m"], "How  Does It WORK?", "x") == cache.key("ask_m3", ["m"], "how does it work?", "x")
    # different context -> different key (same question against different code must miss)
    assert cache.key("ask_m3", ["m"], "q", "code A") != cache.key("ask_m3", ["m"], "q", "code B")
    # different tool / model set -> different key
    assert cache.key("ask_m3", ["m"], "q", "c") != cache.key("ask_glm", ["m"], "q", "c")
    assert cache.key("council", ["a", "b"], "q", "c") == cache.key("council", ["b", "a"], "q", "c")  # order-insensitive


def test_key_separates_effort_levels():
    # A "quick" answer must never be served for a "deep" request on the same question.
    assert cache.key("ask_atlas", ["m"], "q", "c", effort="quick") != cache.key("ask_atlas", ["m"], "q", "c", effort="deep")
    # Effort is case/whitespace-insensitive; omitted and empty are the same key.
    assert cache.key("ask_atlas", ["m"], "q", "c", effort=" Deep ") == cache.key("ask_atlas", ["m"], "q", "c", effort="deep")
    assert cache.key("ask_m3", ["m"], "q", "c") == cache.key("ask_m3", ["m"], "q", "c", effort="")


def test_key_version_scopes_the_key(monkeypatch):
    # Bumping _KEY_VERSION must invalidate existing entries (a semantics/payload
    # change must not serve stale-shaped answers), so the same inputs hash differently.
    k1 = cache.key("council", ["a", "b"], "q", "c")
    monkeypatch.setattr(cache, "_KEY_VERSION", cache._KEY_VERSION + 1)
    assert cache.key("council", ["a", "b"], "q", "c") != k1


def test_put_get_roundtrip(_cache_on):
    k = cache.key("ask_m3", ["MiniMax-M3"], "trace routing", "ctx")
    assert cache.get(k) is None
    cache.put(k, {"status": "ok", "answer": "hi"})
    hit = cache.get(k)
    assert hit is not None
    payload, age = hit
    assert payload == {"status": "ok", "answer": "hi"} and age >= 0


def test_disabled_never_hits(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CACHE", "0")
    k = cache.key("ask_m3", ["m"], "q", "c")
    cache.put(k, {"status": "ok"})
    assert cache.get(k) is None


def test_ttl_expiry(monkeypatch, _cache_on):
    monkeypatch.setenv("ASK_FABLE_CACHE_TTL", "0")  # everything is immediately stale
    k = cache.key("ask_m3", ["m"], "q", "c")
    cache.put(k, {"status": "ok"})
    assert cache.get(k) is None


def test_handler_serves_second_call_from_cache(monkeypatch, _cache_on):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))

    calls = []

    async def fake_run(question, context="", **kw):
        calls.append(question)
        return OracleResult("ok", text="MiniMax says hi", model="MiniMax-M3")

    monkeypatch.setattr(server.minimax, "run", fake_run)

    first = _run(server._handle_m3({"question": "How does routing work?", "context": "def r(): ..."}))
    assert first["answer"] == "MiniMax says hi" and "cached" not in first

    second = _run(server._handle_m3({"question": "How does routing work?", "context": "def r(): ..."}))
    assert second["answer"] == "MiniMax says hi"
    assert second["cached"] is True and "cache_age_s" in second and "near-identical" in second["note"]
    assert len(calls) == 1  # the model was only called once


def test_individual_oracle_caching(monkeypatch, _cache_on):
    import ask_fable.oracles as oracles

    calls = []
    async def fake_minimax_run(question, context="", **kw):
        calls.append(question)
        return OracleResult("ok", text="MiniMax answer text", model="MiniMax-M3")

    monkeypatch.setattr(server.minimax, "run", fake_minimax_run)

    # 1. Run minimax through oracles.run
    res1 = _run(oracles.run("minimax", "question text", "context text"))
    assert res1.text == "MiniMax answer text"
    assert len(calls) == 1

    # 2. Run minimax again through oracles.run with same question/context
    res2 = _run(oracles.run("minimax", "question text", "context text"))
    assert res2.text == "MiniMax answer text"
    assert len(calls) == 1  # No new call, served from cache!


def test_open_breaker_does_not_block_cache_hits(monkeypatch, _cache_on):
    """A cache hit needs no backend call, so an OPEN circuit breaker must not gate
    it — the breaker sheds load from a struggling backend; the cache path adds none."""
    import ask_fable.health as health
    import ask_fable.oracles as oracles

    calls = []
    async def fake_minimax_run(question, context="", **kw):
        calls.append(question)
        return OracleResult("ok", text="cached-worthy answer", model="MiniMax-M3")

    monkeypatch.setattr(server.minimax, "run", fake_minimax_run)
    health.breaker.reset("minimax")

    # Prime the oracle cache with a real (stubbed) call.
    res1 = _run(oracles.run("minimax", "breaker question", "ctx"))
    assert res1.status == "ok" and len(calls) == 1

    # Force the breaker open for minimax.
    for _ in range(6):
        health.breaker.record("minimax", "error", "timeout")
    assert health.breaker.should_skip("minimax") is True

    # The cached answer is still served; a MISS is what gets circuit_open.
    res2 = _run(oracles.run("minimax", "breaker question", "ctx"))
    assert res2.status == "ok" and res2.text == "cached-worthy answer"
    assert len(calls) == 1  # backend untouched
    res3 = _run(oracles.run("minimax", "different uncached question", "ctx"))
    assert res3.status == "error" and res3.kind == "circuit_open"
    health.breaker.reset("minimax")


def test_unicode_normalization_hits_cache(monkeypatch, _cache_on):
    """NFC vs NFD equivalent questions should produce the same cache key."""
    import unicodedata
    nfkc = unicodedata.normalize("NFKC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert nfkc != nfd  # different byte representations
    k1 = cache.key("ask_m3", ["m"], nfkc, "c")
    k2 = cache.key("ask_m3", ["m"], nfd, "c")
    assert k1 == k2  # same key — NFKC normalization before hashing


def test_cache_sweep_evicts_expired_rows(monkeypatch, tmp_path):
    """put() periodically sweeps TTL-expired rows."""
    monkeypatch.setenv("ASK_FABLE_CACHE", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("ASK_FABLE_CACHE_TTL", "0")  # everything immediately stale
    import ask_fable.cache as cache_mod
    # Reset sweep counter
    cache_mod._sweep_counter = 99
    k = cache.key("ask_m3", ["m"], "q", "c")
    cache.put(k, {"status": "ok"})
    # The next put triggers the sweep (counter hits 100)
    cache_mod._sweep_counter = 99
    cache.put(cache.key("ask_m3", ["m"], "q2", "c"), {"status": "ok"})
    # The first entry should have been swept (TTL=0 → immediately stale)
    assert cache.get(k) is None


def test_cache_max_rows_cap(monkeypatch, tmp_path):
    """When the row count exceeds ASK_FABLE_CACHE_MAX_ROWS, the oldest 10% are evicted."""
    monkeypatch.setenv("ASK_FABLE_CACHE", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("ASK_FABLE_CACHE_TTL", "999999")  # no TTL eviction
    monkeypatch.setenv("ASK_FABLE_CACHE_MAX_ROWS", "10")
    import ask_fable.cache as cache_mod
    # Fill past the cap
    for i in range(15):
        cache_mod._sweep_counter = 99  # force sweep on every put
        cache.put(cache.key("ask_m3", ["m"], f"q{i}", "c"), {"status": "ok"})
    # Should have trimmed to ~9 rows (90% of 10)
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "cache.db"))
    try:
        count = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
        assert count <= 10, f"expected <= 10 rows after sweep, got {count}"
    finally:
        conn.close()


def test_cache_db_and_sidecars_are_private(monkeypatch, tmp_path):
    """The db and any SQLite WAL/SHM sidecars are 0o600 after a write — SQLite
    creates sidecars at the process umask, so the store must chmod them."""
    import sys
    if sys.platform == "win32":
        pytest.skip("POSIX mode bits")
    monkeypatch.setenv("ASK_FABLE_CACHE", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE_PATH", str(tmp_path / "cache.db"))
    cache.put(cache.key("ask_m3", ["m"], "q", "c"), {"status": "ok"})
    assert (tmp_path / "cache.db").stat().st_mode & 0o777 == 0o600
    # Sidecars may already be checkpointed away by the per-op close — check any
    # that still exist rather than requiring their presence.
    for suffix in ("-wal", "-shm"):
        sidecar = tmp_path / f"cache.db{suffix}"
        if sidecar.exists():
            assert sidecar.stat().st_mode & 0o777 == 0o600, f"{suffix} not private"
