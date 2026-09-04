"""Theme H — the stats aggregator + tool, audit enrichment, and the RAW split."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import ask_fable.audit as audit
import ask_fable.server as server
import ask_fable.stats as stats


def _run(coro):
    return asyncio.run(coro)


def _ts(hours_ago: float = 0.0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _rec(**kw) -> str:
    base = {
        "ts": _ts(),
        "decision": "allowed",
        "session": "default",
        "model": "claude-fable-5",
        "duration_ms": 100,
    }
    base.update(kw)
    return json.dumps(base) + "\n"


def _write_log(path, lines):
    path.write_text("".join(lines), encoding="utf-8")
    return path


def test_aggregate_buckets_by_model(tmp_path):
    log = _write_log(
        tmp_path / "d.jsonl",
        [
            _rec(model="glm-5.2", duration_ms=100),
            _rec(model="glm-5.2", decision="error", duration_ms=300),
            _rec(model="MiniMax-M3", duration_ms=200),
            _rec(model="MiniMax-M3", decision="refused", duration_ms=None),
            _rec(
                model="MiniMax-M3", decision="denied", duration_ms=None
            ),  # guard denial -> refused
        ],
    )
    out = stats.aggregate(log, window="24h", by="model")
    assert out["status"] == "ok" and out["window"] == "24h" and out["by"] == "model"
    by_key = {b["key"]: b for b in out["buckets"]}
    glm, m3 = by_key["glm-5.2"], by_key["MiniMax-M3"]
    assert glm["calls"] == 2 and glm["errors"] == 1 and glm["error_rate"] == 0.5
    assert glm["avg_ms"] == 200 and glm["p95_ms"] == 300
    assert m3["calls"] == 3 and m3["allowed"] == 1 and m3["refused"] == 2 and m3["errors"] == 0
    assert out["totals"]["calls"] == 5 and out["totals"]["errors"] == 1
    # sorted by traffic: m3 (3 calls) first
    assert out["buckets"][0]["key"] == "MiniMax-M3"


def test_aggregate_window_excludes_old_records(tmp_path):
    log = _write_log(
        tmp_path / "d.jsonl",
        [
            _rec(ts=_ts(hours_ago=0.5)),
            _rec(ts=_ts(hours_ago=30)),  # outside 24h
        ],
    )
    out = stats.aggregate(log, window="24h")
    assert out["totals"]["calls"] == 1
    assert stats.aggregate(log, window="all")["totals"]["calls"] == 2


def test_aggregate_reads_rotation_files_and_skips_junk(tmp_path):
    log = tmp_path / "d.jsonl"
    _write_log(log, [_rec(model="a")])
    _write_log(tmp_path / "d.jsonl.1", [_rec(model="a"), "NOT JSON\n", '["a list, not a dict"]\n'])
    _write_log(tmp_path / "d.jsonl.2", [_rec(model="b")])
    out = stats.aggregate(log, window="all")
    assert out["totals"]["calls"] == 3  # junk lines skipped, rotations included
    assert {b["key"] for b in out["buckets"]} == {"a", "b"}


def test_aggregate_filters_and_day_buckets(tmp_path):
    log = _write_log(
        tmp_path / "d.jsonl",
        [
            _rec(model="a", session="s1"),
            _rec(model="b", session="s2"),
        ],
    )
    assert stats.aggregate(log, window="all", model_filter="a")["totals"]["calls"] == 1
    assert stats.aggregate(log, window="all", session_filter="s2")["totals"]["calls"] == 1
    day = stats.aggregate(log, window="all", by="day")
    assert day["buckets"][0]["key"] == _ts()[:10]


def test_aggregate_missing_log_is_empty_not_error(tmp_path):
    out = stats.aggregate(tmp_path / "nope.jsonl")
    assert out["status"] == "ok" and out["buckets"] == [] and out["totals"]["calls"] == 0


def test_handle_stats_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    log = _write_log(tmp_path / "d.jsonl", [_rec(model="glm-5.2", decision="error")])
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(log))
    out = _run(server._handle_stats({"window": "24h", "by": "model"}))
    assert out["status"] == "ok"
    assert out["buckets"][0]["key"] == "glm-5.2" and out["buckets"][0]["errors"] == 1
    # bogus enum values fall back to defaults rather than erroring
    out2 = _run(server._handle_stats({"window": "nonsense", "by": "bogus"}))
    assert out2["window"] == "24h" and out2["by"] == "model"


# ---------- audit enrichment (quorum/consensus/synth_fallback) ----------


def test_audit_records_council_enrichment(monkeypatch, tmp_path):
    log = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(log))
    audit.record(
        decision="allowed",
        stage=None,
        reason="council_synth",
        question="q",
        quorum="2/3",
        consensus="strong",
        synth_fallback=False,
    )
    audit.record(decision="allowed", stage=None, reason="ok", question="q")  # single tool
    recs = [json.loads(x) for x in log.read_text().splitlines()]
    assert recs[0]["quorum"] == "2/3" and recs[0]["consensus"] == "strong"
    assert recs[0]["synth_fallback"] is False
    # single-tool records stay slim — no council keys at all
    assert (
        "quorum" not in recs[1] and "consensus" not in recs[1] and "synth_fallback" not in recs[1]
    )


# ---------- ASK_FABLE_AUDIT_RAW_CONTEXT split ----------


def test_audit_raw_context_split(monkeypatch, tmp_path):
    log = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(log))
    monkeypatch.setenv("ASK_FABLE_AUDIT_RAW", "1")
    monkeypatch.setenv("ASK_FABLE_AUDIT_RAW_CONTEXT", "0")
    audit.record(
        decision="allowed",
        stage=None,
        reason="ok",
        question="the question",
        context="proprietary code",
    )
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["question_raw"] == "the question"  # question kept for debugging
    assert "context_raw" not in rec  # context stays hashed-only
    assert rec["context_len"] == len("proprietary code")


def test_audit_raw_context_defaults_to_raw_switch(monkeypatch, tmp_path):
    log = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(log))
    monkeypatch.setenv("ASK_FABLE_AUDIT_RAW", "1")
    monkeypatch.delenv("ASK_FABLE_AUDIT_RAW_CONTEXT", raising=False)
    audit.record(decision="allowed", stage=None, reason="ok", question="q", context="c")
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["question_raw"] == "q" and rec["context_raw"] == "c"  # single switch still works


def test_council_passes_enrichment_to_audit(monkeypatch):
    """_council must hand quorum/consensus/synth_fallback to audit.record."""
    from ask_fable.oracle_common import OracleResult

    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    seen = {}
    monkeypatch.setattr(server.audit, "record", lambda **kw: seen.update(kw))

    async def fake_fable(question, context="", *, resume=None, system_prompt=None, on_think=None):
        return OracleResult("ok", text="fable says X")

    async def fake_minimax(question, context="", **kw):
        return OracleResult("ok", text="minimax says X", model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)

    out = _run(
        server._council(
            question="Q?",
            context="",
            selected=["fable", "minimax"],
            unknown=[],
            title="ask_council",
            ref_resolved=[],
            ref_missing=[],
        )
    )
    assert out["status"] == "ok"
    assert seen["quorum"] == "2/2"
    assert seen["consensus"] in ("strong", "partial", "divergent", "unknown")
    assert seen["synth_fallback"] is False


def test_v2_tool_events_support_tool_and_cache_metrics(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:00Z","event_name":"tool.completed",'
        '"tool":"ask_codex","status":"ok","duration_ms":20,"cache":{"status":"hit"},'
        '"usage":{"input_tokens":4,"output_tokens":6,"total_tokens":10,"cost_usd":0.01}}\n'
    )
    out = stats.aggregate(path, window="all", by="tool")
    assert out["buckets"][0]["key"] == "ask_codex"
    assert out["totals"]["cache_hits"] == 1
    assert out["totals"]["total_tokens"] == 10
    assert out["totals"]["cost_usd"] == 0.01
    assert out["totals"]["cache_hit_rate"] == 1.0


def test_v2_root_supersedes_same_period_legacy_and_reads_immutable_rotation(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text('{"ts":"2026-07-11T00:00:01Z","decision":"allowed","model":"codex"}\n')
    rotated = tmp_path / "decisions.20260711T000002000000Z.000000.jsonl"
    rotated.write_text(
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:00Z","event_name":"tool.completed",'
        '"tool":"ask_codex","status":"ok","duration_ms":20}\n'
    )
    out = stats.aggregate(path, window="all", by="tool")
    assert out["totals"]["calls"] == 1
    assert out["buckets"][0]["key"] == "ask_codex"


def test_cache_hit_rate_uses_only_hits_and_misses(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:00Z","event_name":"tool.completed",'
        '"tool":"a","status":"ok","cache":{"status":"hit"}}\n'
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:01Z","event_name":"tool.completed",'
        '"tool":"a","status":"ok","cache":{"status":"miss"}}\n'
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:02Z","event_name":"tool.completed",'
        '"tool":"b","status":"ok"}\n'
    )
    out = stats.aggregate(path, window="all", by="tool")
    assert out["totals"]["cache_hit_rate"] == 0.5
    assert (
        next(bucket for bucket in out["buckets"] if bucket["key"] == "b")["cache_hit_rate"] == 0.0
    )


def test_aggregate_by_mode_reads_orchestration_mode(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:00Z","event_name":"tool.completed",'
        '"tool":"ask_council","status":"ok","duration_ms":20,"mode":"safe",'
        '"orchestration":{"mode":"council"}}\n'
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:01Z","event_name":"tool.completed",'
        '"tool":"ask_chain","status":"ok","duration_ms":30,"orchestration":{"mode":"chain"}}\n'
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:02Z","event_name":"tool.completed",'
        '"tool":"ask","status":"ok","duration_ms":10,"mode":"full",'
        '"orchestration":{"output":{"sha256":"x","chars":1,"bytes":1},"model":"claude-fable-5"}}\n'
    )
    out = stats.aggregate(path, window="all", by="mode")
    by_key = {b["key"]: b for b in out["buckets"]}
    assert by_key["council"]["calls"] == 1
    assert by_key["chain"]["calls"] == 1
    # Single-model calls carry a top-level "mode" (trace CAPTURE mode, safe/full)
    # and an orchestration dict without "mode" — they must land in "?", not in a
    # "safe"/"full" bucket and not one literally named "None".
    assert by_key["?"]["calls"] == 1
    assert not {"None", "safe", "full"} & by_key.keys()
    assert out["totals"]["calls"] == 3


def test_aggregate_quorum_consensus_synth_fallback(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:00Z","event_name":"tool.completed",'
        '"tool":"ask_council","status":"ok","duration_ms":20,'
        '"orchestration":{"mode":"council","quorum":"2/3","consensus":"strong","synth_fallback":false}}\n'
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:01Z","event_name":"tool.completed",'
        '"tool":"ask_council","status":"ok","duration_ms":30,'
        '"orchestration":{"mode":"council","quorum":"1/3","consensus":"partial","synth_fallback":true}}\n'
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:02Z","event_name":"tool.completed",'
        '"tool":"ask","status":"ok","duration_ms":10}\n'
    )
    out = stats.aggregate(path, window="all", by="mode")
    by_key = {b["key"]: b for b in out["buckets"]}
    council = by_key["council"]
    assert council["consensus_counts"] == {"strong": 1, "partial": 1}
    assert council["synth_fallback_true"] == 1
    assert by_key["?"]["consensus_counts"] == {}
    assert by_key["?"]["synth_fallback_true"] == 0
    assert out["totals"]["synth_fallback_true"] == 1


def test_durationless_rotated_bucket_does_not_inherit_latency(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        '{"ts":"2026-07-11T00:00:01Z","decision":"allowed","model":"legacy-no-duration"}\n'
    )
    (tmp_path / "decisions.jsonl.1").write_text(
        '{"ts":"2026-07-11T00:00:00Z","decision":"allowed","model":"timed","duration_ms":10}\n'
    )
    out = stats.aggregate(path, window="all", by="model")
    buckets = {bucket["key"]: bucket for bucket in out["buckets"]}
    assert buckets["legacy-no-duration"]["avg_ms"] is None
    assert buckets["timed"]["avg_ms"] == 10


def test_provider_buckets_keep_breaker_shed_calls_out_of_errors(tmp_path):
    """A circuit_open skip touched no backend: it must not count as an error or
    as a ~0 ms latency sample — that made a tripped oracle look both worse and
    faster than it was for the whole cooldown."""
    path = tmp_path / "decisions.jsonl"

    def ev(status, ms, kind=None):
        rec = {
            "schema_version": 2,
            "timestamp": _ts(),
            "event_name": "provider.completed",
            "tool": "ask_council",
            "status": status,
            "duration_ms": ms,
            "provider": {"oracle_key": "glm", "actual_model": "GLM-5.2", "transport": "bridge"},
        }
        if kind:
            rec["error"] = {"category": status, "kind": kind}
        return json.dumps(rec) + "\n"

    path.write_text(
        ev("ok", 900) + ev("error", 700, "timeout") + ev("error", 0, "circuit_open") * 3
    )
    out = stats.aggregate(path, window="all", by="provider")
    (b,) = out["buckets"]
    assert b["key"] == "GLM-5.2"
    assert b["calls"] == 2 and b["errors"] == 1 and b["error_rate"] == 0.5
    assert b["circuit_open"] == 3
    assert b["avg_ms"] == 800  # the three 0 ms skips are not latency
    assert out["totals"]["circuit_open"] == 3 and out["totals"]["calls"] == 2


def test_fully_shed_oracle_sorts_by_traffic_not_calls(tmp_path):
    """An oracle whose breaker was open all window has calls=0. Sorting on calls
    alone buried the one dead backend under every healthy one."""
    path = tmp_path / "decisions.jsonl"

    def ev(model, status, ms, kind=None):
        rec = {
            "schema_version": 2, "timestamp": _ts(), "event_name": "provider.completed",
            "tool": "ask_council", "status": status, "duration_ms": ms,
            "provider": {"oracle_key": model, "actual_model": model, "transport": "bridge"},
        }
        if kind:
            rec["error"] = {"category": status, "kind": kind}
        return json.dumps(rec) + "\n"

    path.write_text(ev("GLM-5.2", "error", 0, "circuit_open") * 20 + ev("MiniMax-M3", "ok", 800) * 3)
    out = stats.aggregate(path, window="all", by="provider")
    assert [b["key"] for b in out["buckets"]] == ["GLM-5.2", "MiniMax-M3"]
    shed = out["buckets"][0]
    assert shed["calls"] == 0 and shed["errors"] == 0 and shed["circuit_open"] == 20


def test_shed_call_still_counts_its_cache_lookup(tmp_path):
    """A shed call is excluded from calls/errors/latency but it DID do an outer
    cache lookup; dropping those inflated the hit rate over a fraction of the
    real lookups."""
    path = tmp_path / "decisions.jsonl"

    def ev(kind=None):
        rec = {
            "schema_version": 2, "timestamp": _ts(), "event_name": "tool.completed",
            "tool": "ask_glm", "status": "error" if kind else "ok", "duration_ms": 5,
            "cache": {"status": "miss"}, "orchestration": {"model": "GLM-5.2"},
        }
        if kind:
            rec["error"] = {"category": "error", "kind": kind}
        return json.dumps(rec) + "\n"

    path.write_text(ev() + ev("circuit_open") * 3)
    assert stats.aggregate(path, window="all", by="cache")["buckets"][0]["cache_misses"] == 4
    out = stats.aggregate(path, window="all", by="model")
    # And the shed calls land in the oracle's OWN bucket, not just in totals.
    assert out["buckets"][0]["key"] == "GLM-5.2"
    assert out["buckets"][0]["circuit_open"] == 3
    assert out["totals"]["circuit_open"] == sum(b["circuit_open"] for b in out["buckets"])


def test_cancelled_is_not_a_backend_error_and_shed_shows_a_rate(tmp_path):
    """Two things error_rate cannot say. A call the caller's cap cancelled has
    real latency but is not the backend failing — the same reason the circuit
    breaker ignores it. And an oracle shed for the whole window has calls=0, so
    error_rate stays 0.0 no matter how dead it is; shed_rate is what reads red."""
    path = tmp_path / "decisions.jsonl"

    def ev(model, status, ms, kind=None):
        rec = {
            "schema_version": 2, "timestamp": _ts(), "event_name": "provider.completed",
            "tool": "ask_council", "status": status, "duration_ms": ms,
            "provider": {"oracle_key": model, "actual_model": model, "transport": "bridge"},
        }
        if kind:
            rec["error"] = {"category": status, "kind": kind}
        return json.dumps(rec) + "\n"

    path.write_text(
        ev("GLM-5.2", "error", 0, "circuit_open") * 20
        + ev("Slow", "error", 900, "cancelled") * 4
        + ev("Slow", "ok", 800)
    )
    buckets = {b["key"]: b for b in stats.aggregate(path, window="all", by="provider")["buckets"]}

    slow = buckets["Slow"]
    assert slow["calls"] == 5 and slow["cancelled"] == 4
    assert slow["errors"] == 0 and slow["error_rate"] == 0.0  # local cap, not the backend
    assert slow["avg_ms"] == 880  # the latency was real

    dead = buckets["GLM-5.2"]
    assert dead["calls"] == 0 and dead["errors"] == 0 and dead["error_rate"] == 0.0
    assert dead["circuit_open"] == 20 and dead["shed_rate"] == 1.0
