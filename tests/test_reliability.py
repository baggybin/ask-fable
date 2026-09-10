"""Atomic write behavior of outputs.save and the new chain/council timeouts."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import ask_fable.cache as cache
import ask_fable.context_store as cs
import ask_fable.oracles as oracles
import ask_fable.outputs as outputs
import ask_fable.server as server
from ask_fable.oracle_common import OracleResult

# ---------- atomic outputs.save ----------


def test_outputs_save_creates_file_at_0o600(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_SAVE", "1")
    monkeypatch.setenv("ASK_FABLE_OUTPUT_DIR", str(tmp_path / "answers"))
    p = outputs.save(tool="ask_m3", model="MiniMax-M3", question="q", answer="a")
    assert p is not None
    file = Path(p)
    assert file.exists()
    if sys.platform != "win32":
        mode = file.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_outputs_save_no_partial_on_simulated_crash(monkeypatch, tmp_path):
    """A crash mid-rename must not leave a partial markdown file behind."""
    monkeypatch.setenv("ASK_FABLE_SAVE", "1")
    monkeypatch.setenv("ASK_FABLE_OUTPUT_DIR", str(tmp_path / "answers"))
    # Patch os.replace to raise.
    import ask_fable._paths as paths_mod
    def boom(src, dst):
        raise OSError("simulated rename failure")
    monkeypatch.setattr(paths_mod.os, "replace", boom)
    p = outputs.save(tool="ask_m3", model="MiniMax-M3", question="q", answer="a")
    assert p is None  # the save failed, no path returned
    # No target file written.
    targets = list((tmp_path / "answers").glob("*.md"))
    assert targets == []


def test_outputs_save_creates_dir_with_0o700(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_SAVE", "1")
    monkeypatch.setenv("ASK_FABLE_OUTPUT_DIR", str(tmp_path / "answers"))
    outputs.save(tool="ask_m3", model="MiniMax-M3", question="q", answer="a")
    d = tmp_path / "answers"
    if sys.platform != "win32":
        mode = d.stat().st_mode & 0o777
        assert mode == 0o700


# ---------- cache WAL + 0o600 ----------


def test_cache_db_created_at_0o600(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_CACHE", "1")
    p = tmp_path / "cache.db"
    monkeypatch.setenv("ASK_FABLE_CACHE_PATH", str(p))
    cache.put(cache.key("ask_m3", ["m"], "q", "c"), {"status": "ok", "answer": "a"})
    if sys.platform != "win32":
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_cache_uses_wal(monkeypatch, tmp_path):
    """The cache should be in WAL mode for crash safety."""
    import sqlite3
    monkeypatch.setenv("ASK_FABLE_CACHE", "1")
    p = tmp_path / "cache.db"
    monkeypatch.setenv("ASK_FABLE_CACHE_PATH", str(p))
    cache.put(cache.key("ask_m3", ["m"], "q", "c"), {"status": "ok", "answer": "a"})
    conn = sqlite3.connect(str(p))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal", f"expected WAL, got {mode}"
    finally:
        conn.close()


# ---------- context_store WAL + 0o600 ----------


def test_context_db_created_at_0o600(monkeypatch, tmp_path):
    p = tmp_path / "context.db"
    monkeypatch.setenv("ASK_FABLE_CONTEXT_PATH", str(p))
    cs.put("k", "v")
    if sys.platform != "win32":
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600


def test_context_store_uses_wal(monkeypatch, tmp_path):
    import sqlite3
    p = tmp_path / "context.db"
    monkeypatch.setenv("ASK_FABLE_CONTEXT_PATH", str(p))
    cs.put("k", "v")
    conn = sqlite3.connect(str(p))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


# ---------- council: gather return_exceptions + synthetic OracleResult ----------


def test_council_synthesizes_when_a_bridge_raises(monkeypatch, tmp_path):
    """A bridge raising mid-fan-out must NOT kill the gather — the other
    oracles' answers must still flow through to synthesis."""
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))

    # fable.run will raise — the per-bridge contract says it shouldn't, but
    # we want gather(return_exceptions=True) to handle the regression case.
    async def fake_fable_run(question, context="", **kw):
        raise RuntimeError("simulated bridge failure")

    monkeypatch.setattr(server.fable, "run", fake_fable_run)

    # minimax.run returns a normal answer
    async def fake_minimax_run(question, context="", **kw):
        return OracleResult("ok", text="minimax answer", model="MiniMax-M3")

    monkeypatch.setattr(server.minimax, "run", fake_minimax_run)

    out = asyncio.run(server._council(
        question="Q?", context="", selected=["fable", "minimax"], unknown=[],
        title="ask_council", ref_resolved=[], ref_missing=[],
    ))
    # The error from fable becomes a synthetic OracleResult; minimax's ok answer
    # is the only real one, so we get a 1-of-2 council result.
    assert out["status"] == "ok"
    assert out["answered_by"] == "MiniMax-M3"
    # A synthetic entry from the bridge failure is present — attributed to its REAL
    # oracle key ("fable"), not collapsed into a shared "<exception>" placeholder.
    assert "fable" in out["sources"]
    assert out["sources"]["fable"]["kind"] == "sdk_error"
    assert "simulated bridge failure" in out["sources"]["fable"].get("detail", "")


def test_council_timeout_preserves_completed_oracles(monkeypatch, tmp_path):
    """A PARTIAL timeout must synthesize the oracles that answered rather than discard
    the whole batch — only the still-running one is reported as a per-oracle timeout."""
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_COUNCIL_TIMEOUT", "1")  # 1 second
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))

    async def fast_fable(question, context="", **kw):
        return OracleResult("ok", text="fable answered in time", session_id=None)

    async def slow_minimax(question, context="", **kw):
        await asyncio.sleep(5)  # exceeds the 1s cap
        return OracleResult("ok", text="too slow", model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", fast_fable)
    monkeypatch.setattr(server.minimax, "run", slow_minimax)

    out = asyncio.run(server._council(
        question="Q?", context="", selected=["fable", "minimax"], unknown=[],
        title="ask_council", ref_resolved=[], ref_missing=[],
    ))
    # The council still succeeds on fable's answer; minimax shows as its own timeout.
    assert out["status"] == "ok"
    assert out["sources"]["fable"]["status"] == "ok"
    assert out["sources"]["minimax"]["kind"] == "timeout"


def test_council_overall_timeout_fires(monkeypatch, tmp_path):
    """If the whole fan-out exceeds the timeout, surface a structured error."""
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_COUNCIL_TIMEOUT", "1")  # 1 second
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))

    async def slow_fable_run(question, context="", **kw):
        await asyncio.sleep(5)  # longer than the 1s timeout
        return OracleResult("ok", text="slow", session_id=None)

    async def slow_minimax_run(question, context="", **kw):
        await asyncio.sleep(5)
        return OracleResult("ok", text="slow", model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", slow_fable_run)
    monkeypatch.setattr(server.minimax, "run", slow_minimax_run)

    out = asyncio.run(server._council(
        question="Q?", context="", selected=["fable", "minimax"], unknown=[],
        title="ask_council", ref_resolved=[], ref_missing=[],
    ))
    assert out["status"] == "error"
    assert out["kind"] == "timeout"
    assert "ASK_FABLE_COUNCIL_TIMEOUT" in out["detail"]
    # Both oracles should appear in sources as errors.
    assert "fable" in out["sources"]
    assert "minimax" in out["sources"]
    assert out["sources"]["fable"]["kind"] == "timeout"


def test_council_semaphore_caps_parallel(monkeypatch, tmp_path):
    """The fan-out semaphore bounds the simultaneous oracle count."""
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_MAX_PARALLEL", "2")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))

    in_flight = 0
    peak_in_flight = 0

    async def track_in_flight(q, c="", **kw):
        nonlocal in_flight, peak_in_flight
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return OracleResult("ok", text="ok", model=q)

    async def track_fable(question, context="", **kw):
        nonlocal in_flight, peak_in_flight
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return OracleResult("ok", text="ok", session_id=None)

    monkeypatch.setattr(server.minimax, "run", track_in_flight)
    monkeypatch.setattr(server.gemini, "run", track_in_flight)
    monkeypatch.setattr(server.fable, "run", track_fable)

    out = asyncio.run(server._council(
        question="Q?", context="",
        selected=["fable", "minimax", "gemini"],
        unknown=[], title="ask_council", ref_resolved=[], ref_missing=[],
    ))
    assert out["status"] == "ok"
    # 3 oracles but a cap of 2 → peak should never exceed 2.
    assert peak_in_flight <= 2, f"semaphore did not cap concurrency; peak={peak_in_flight}"


# ---------- chain timeout ----------


def test_chain_timeout_fires(monkeypatch, tmp_path):
    """A chain that exceeds its overall timeout surfaces a structured error with partial stages."""
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_CHAIN_TIMEOUT", "1")  # 1 second overall
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))

    async def slow_minimax_run(question, context="", **kw):
        await asyncio.sleep(5)  # longer than 1s timeout
        return OracleResult("ok", text="minimax", model="MiniMax-M3")

    async def slow_fable_run(question, context="", **kw):
        await asyncio.sleep(5)
        return OracleResult("ok", text="fable", session_id=None)

    monkeypatch.setattr(server.minimax, "run", slow_minimax_run)
    monkeypatch.setattr(server.fable, "run", slow_fable_run)

    out = asyncio.run(server._chain(
        question="Q?", context="", selected=["minimax", "fable"],
        unknown=[], title="ask_chain", ref_resolved=[], ref_missing=[],
    ))
    assert out["status"] == "error"
    assert out["kind"] == "timeout"
    assert "ASK_FABLE_CHAIN_TIMEOUT" in out["detail"]
    # Pipeline should still be reported.
    assert "pipeline" in out
    assert out["requested"] == 2


def test_council_timeout_records_member_provider_event(monkeypatch, tmp_path):
    """A member the wall-clock cap cancels used to leave no provider.completed
    event at all — invisible to stats(by="provider"), the only view that sees
    panelists individually. oracles.run now records the attempt as it is
    cancelled, so the slow member is the one that shows up."""
    from ask_fable import trace_runtime

    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_COUNCIL_TIMEOUT", "1")
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))

    async def fast_fable(question, context="", **kw):
        return OracleResult("ok", text="fable answered in time", session_id=None)

    async def slow_minimax(question, context="", **kw):
        await asyncio.sleep(5)
        return OracleResult("ok", text="too slow", model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", fast_fable)
    monkeypatch.setattr(server.minimax, "run", slow_minimax)

    async def go():
        with trace_runtime.tool_trace("ask_council", {"question": "Q?"}) as trace:
            out = await server._council(
                question="Q?", context="", selected=["fable", "minimax"], unknown=[],
                title="ask_council", ref_resolved=[], ref_missing=[],
            )
            trace.complete(out)
        return out

    out = asyncio.run(go())
    assert out["sources"]["minimax"]["kind"] == "timeout"
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    members = {
        e["provider"]["oracle_key"]: e for e in events if e["event_name"] == "provider.completed"
    }
    assert members["fable"]["status"] == "ok"
    assert members["minimax"]["status"] == "error"
    assert members["minimax"]["error"]["kind"] == "cancelled"
    assert members["minimax"]["duration_ms"] >= 900  # ~the 1s cap it waited for
    assert members["minimax"]["provider"]["actual_model"] == oracles.label("minimax")


def test_chain_timeout_records_in_flight_member(monkeypatch, tmp_path):
    """The same invariant for chains. Council was only one of three orchestrators
    that cap themselves by cancelling an in-flight oracles.run; recording the
    attempt inside run() is what makes "every attempted oracle leaves a
    provider.completed" true for chain and debate too, with no per-mode code."""
    from ask_fable import trace_runtime

    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    # ASK_FABLE_CHAIN_TIMEOUT floors at 10s, too slow for a unit test — override
    # the cap itself so the in-flight stage is cancelled after 1s.
    monkeypatch.setattr(server, "_chain_timeout_s", lambda n: 1.0)
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))

    async def fast_minimax(question, context="", **kw):
        return OracleResult("ok", text="stage one", model="MiniMax-M3")

    async def slow_fable(question, context="", **kw):
        await asyncio.sleep(5)  # still running when the chain cap fires
        return OracleResult("ok", text="never", session_id=None)

    monkeypatch.setattr(server.minimax, "run", fast_minimax)
    monkeypatch.setattr(server.fable, "run", slow_fable)

    async def go():
        with trace_runtime.tool_trace("ask_chain", {"question": "Q?"}) as trace:
            out = await server._chain(
                question="Q?", context="", selected=["minimax", "fable"],
                unknown=[], title="ask_chain", ref_resolved=[], ref_missing=[],
            )
            trace.complete(out)
        return out

    out = asyncio.run(go())
    assert out["status"] == "error" and out["kind"] == "timeout"
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    members = {
        e["provider"]["oracle_key"]: e for e in events if e["event_name"] == "provider.completed"
    }
    assert members["minimax"]["status"] == "ok"  # the stage that finished
    assert members["fable"]["status"] == "error"  # the stage the cap cancelled
    assert members["fable"]["error"]["kind"] == "cancelled"


def test_cancelled_call_does_not_trip_the_circuit_breaker(monkeypatch):
    """Cancelling a slow oracle is our impatience, not backend ill-health — a
    council that keeps timing out must not also open the breaker on its members."""
    import ask_fable.health as health

    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_BREAKER_WINDOW", "5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_THRESHOLD", "0.5")

    async def slow_minimax(question, context="", **kw):
        await asyncio.sleep(5)
        return OracleResult("ok", text="never", model="MiniMax-M3")

    monkeypatch.setattr(server.minimax, "run", slow_minimax)

    async def go():
        for _ in range(6):
            task = asyncio.create_task(oracles.run("minimax", "q", ""))
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert health.breaker.state("minimax") == "closed"


def test_council_records_member_cancelled_while_still_queued(monkeypatch, tmp_path):
    """The fan-out semaphore means a member can be cancelled BEFORE oracles.run is
    entered, where the capture inside run cannot speak for it. With a cap of one,
    the second member never starts — and must still leave a provider event."""
    from ask_fable import trace_runtime

    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_COUNCIL_TIMEOUT", "1")
    monkeypatch.setenv("ASK_FABLE_MAX_PARALLEL", "1")  # second member queues
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))

    async def slow(question, context="", **kw):
        await asyncio.sleep(5)
        return OracleResult("ok", text="never", session_id=None)

    monkeypatch.setattr(server.fable, "run", slow)
    monkeypatch.setattr(server.minimax, "run", slow)

    async def go():
        with trace_runtime.tool_trace("ask_council", {"question": "Q?"}) as trace:
            out = await server._council(
                question="Q?", context="", selected=["fable", "minimax"], unknown=[],
                title="ask_council", ref_resolved=[], ref_missing=[],
            )
            trace.complete(out)
        return out

    asyncio.run(go())
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    members = {
        e["provider"]["oracle_key"]: e for e in events if e["event_name"] == "provider.completed"
    }
    # Both the one that ran and the one that never got a slot are accounted for.
    assert set(members) == {"fable", "minimax"}
    assert all(e["error"]["kind"] == "cancelled" for e in members.values())
    # ...but the queued one must not be charged as a backend call: no bridge was
    # opened, so it reports `skipped` and no latency, rather than a ~1s sample
    # for a call it never received.
    assert members["minimax"]["provider"]["transport"] == "skipped"
    assert members["minimax"]["duration_ms"] == 0
    assert members["fable"]["provider"]["transport"] == "sdk"
    assert members["fable"]["duration_ms"] >= 900
