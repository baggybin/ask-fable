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


def test_an_answer_cut_off_at_the_output_cap_is_never_cached(monkeypatch, _cache_on):
    """finish_reason "length" must reach the oracle cache as kind="truncated" — the
    one kind it refuses to pin — or the cut-off answer is served for an hour."""
    import io
    import json

    import ask_fable.oracles as oracles

    monkeypatch.setenv("ASK_FABLE_OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(oracles.grok, "available", lambda: False)
    monkeypatch.setattr(oracles.kimi, "available", lambda: False)
    monkeypatch.setattr(oracles.openrouter, "_clamp_effort", lambda *a: None)
    posts = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        posts.append(req.full_url)
        body = {"choices": [{"message": {"content": "Step 1: first do"},
                             "finish_reason": "length"}]}
        return _Resp(json.dumps(body).encode())

    monkeypatch.setattr(oracles.openrouter.urllib.request, "urlopen", fake_urlopen)
    for _ in range(2):
        r = _run(oracles.run("openrouter:x/y", "why does this deadlock?", "code", effort="quick"))
        assert r.status == "ok" and r.kind == "truncated" and r.telemetry.transport != "cache"
    assert len(posts) == 2  # asked again, not served the cut-off answer


def test_one_model_id_on_two_gateways_never_shares_an_answer(monkeypatch, _cache_on):
    """label() drops the gateway prefix, so keying on it alone served Atlas's cached
    answer for openrouter:<same id> — and openai/gpt-5.6-sol is BOTH gateways'
    default synthesizer id."""
    import ask_fable.oracles as oracles

    asked: list[str] = []

    def gateway(name):
        async def run(model, question, context="", **kw):
            asked.append(name)
            return OracleResult("ok", text=f"answer from {name}", model=model)

        return run

    monkeypatch.setattr(oracles.grok, "available", lambda: False)
    monkeypatch.setattr(oracles.kimi, "available", lambda: False)
    for mod in (oracles.atlas, oracles.openrouter):
        monkeypatch.setattr(mod, "configured", lambda: True)
    monkeypatch.setattr(oracles.atlas, "run", gateway("atlas"))
    monkeypatch.setattr(oracles.openrouter, "run", gateway("openrouter"))
    a = _run(oracles.run("atlas:openai/gpt-5.6-sol", "is this migration safe?", "ALTER ..."))
    o = _run(oracles.run("openrouter:openai/gpt-5.6-sol", "is this migration safe?", "ALTER ..."))
    assert a.text == "answer from atlas" and o.text == "answer from openrouter"
    assert asked == ["atlas", "openrouter"]


def test_a_demoted_ladder_answer_is_never_served_as_the_named_model(monkeypatch, _cache_on):
    """A `fable` call is keyed on the label resolved BEFORE the call (5.1). If the
    ladder demotes mid-flight, Fable 5 answers — and pinning that under the 5.1 key
    served it later as 5.1, even to the pinned `fable51`, which must never
    quietly answer as Fable 5."""
    import ask_fable.oracles as oracles

    monkeypatch.setattr(oracles.fable, "fable_model", lambda: "claude-fable-5-1")
    ladder_calls: list[str] = []
    pinned_calls: list[str] = []

    async def ladder(question, context="", **kw):  # 5.1 rejected mid-call; 5 answered
        ladder_calls.append(question)
        return OracleResult("ok", text="answer from claude-fable-5", model="claude-fable-5")

    async def pinned(question, context="", **kw):
        pinned_calls.append(question)
        return OracleResult("ok", text="answer from claude-fable-5-1", model="claude-fable-5-1")

    monkeypatch.setattr(oracles.fable, "run", ladder)
    monkeypatch.setattr(oracles.fable51, "run", pinned)
    q = "Is this double-checked locking pattern safe?"
    assert _run(oracles.run("fable", q, "ctx")).model == "claude-fable-5"
    r = _run(oracles.run("fable51", q, "ctx"))
    assert r.text == "answer from claude-fable-5-1" and pinned_calls == [q]
    # ...nor to `fable` itself while its label still says 5.1 (e.g. a new process).
    assert _run(oracles.run("fable", q, "ctx")).telemetry.transport != "cache"
    assert len(ladder_calls) == 2


def test_a_changed_default_effort_is_not_served_the_old_answer(monkeypatch, _cache_on):
    """A council passes effort=None, i.e. "the operator's default". Keyed as a bare
    None, a `quick` answer kept being served after the default became `deep`."""
    import ask_fable.oracles as oracles

    efforts_run: list[str | None] = []

    async def fake_atlas(model, question, context="", *, effort=None, **kw):
        efforts_run.append(oracles.atlas.default_effort())
        return OracleResult("ok", text="an answer", model=model)

    monkeypatch.setattr(oracles.grok, "available", lambda: False)
    monkeypatch.setattr(oracles.kimi, "available", lambda: False)
    monkeypatch.setattr(oracles.atlas, "configured", lambda: True)
    monkeypatch.setattr(oracles.atlas, "run", fake_atlas)
    monkeypatch.setenv("ASK_FABLE_ATLAS_EFFORT", "quick")
    _run(oracles.run("atlas:acme/m", "why does this deadlock?", "code"))
    monkeypatch.setenv("ASK_FABLE_ATLAS_EFFORT", "deep")
    r = _run(oracles.run("atlas:acme/m", "why does this deadlock?", "code"))
    assert r.telemetry.transport != "cache" and efforts_run == ["quick", "deep"]


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


def test_key_tolerates_lone_surrogates():
    # MCP JSON can carry "\udc80"; hashing it must not raise out of the tool call, and
    # distinct inputs stay distinct keys.
    a = cache.key("ask", ["fable"], "q", "ctx \udc80")
    b = cache.key("ask", ["fable"], "q", "ctx \udc81")
    assert a != b
