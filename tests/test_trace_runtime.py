from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from ask_fable import trace_runtime
from ask_fable.provider_telemetry import ProviderTelemetry, ProviderUsage


def test_trace_scope_decorates_response_and_records_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    with trace_runtime.tool_trace("context_list", {}) as trace:
        payload = trace.complete({"status": "ok", "count": 0})
    assert payload["schema_version"] == 2
    assert payload["trace_id"] == trace.trace_id
    assert payload["telemetry"]["status"] == "ok"
    records = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [record["event_name"] for record in records] == ["tool.started", "tool.completed"]
    assert {record["trace_id"] for record in records} == {trace.trace_id}


def test_set_orchestration_merges_into_tool_completed(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    with trace_runtime.tool_trace("ask_council", {"question": "q"}) as trace:
        trace.set_orchestration(mode="council", models=["fable", "minimax"])
        trace.complete({"status": "ok", "answer": "yes"})
    terminal = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert terminal["event_name"] == "tool.completed"
    assert terminal["orchestration"]["mode"] == "council"
    assert terminal["orchestration"]["models"] == ["fable", "minimax"]
    assert terminal["orchestration"]["output"]["sha256"]


def test_trace_scope_is_fail_open_when_store_rejects_event(monkeypatch):
    monkeypatch.setattr(trace_runtime.EventStore, "append", lambda self, event: False)
    with trace_runtime.tool_trace("context_list", {}) as trace:
        payload = trace.complete({"status": "ok"})
    assert payload["status"] == "ok"
    assert payload["telemetry"]["status"] == "degraded"


def test_cache_payload_drops_stale_artifacts_and_trace_identity():
    cached = {
        "status": "ok",
        "trace_id": "old",
        "duration_ms": 99,
        "saved": "/old/answer.md",
        "artifacts": [{"path": "/old/answer.md"}],
        "telemetry": {"status": "ok"},
    }
    served = trace_runtime.prepare_cache_hit(cached, age_seconds=4)
    assert served["cache"]["source_trace_id"] == "old"
    assert "trace_id" not in served
    assert "saved" not in served
    assert "artifacts" not in served
    assert "telemetry" not in served


def test_terminal_event_contains_cache_usage_artifacts_and_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    with trace_runtime.tool_trace("ask_codex", {"question": "q", "session": "s"}) as trace:
        trace.complete(
            {
                "status": "ok",
                "cache": {"status": "miss"},
                "usage": {"total_tokens": 3},
                "artifacts": [{"kind": "answer", "path": "/tmp/a"}],
            }
        )
    terminal = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert terminal["cache"] == {"status": "miss"}
    assert terminal["usage"] == {"total_tokens": 3}
    assert terminal["artifact"]["items"][0]["kind"] == "answer"
    assert terminal["server_instance_id"]
    assert terminal["session"] == "s"


def test_cache_store_adds_current_origin_trace():
    with trace_runtime.tool_trace("ask_codex", {}) as trace:
        stored = trace_runtime.prepare_cache_store({"status": "ok", "saved": "stale"})
    assert stored["origin_trace_id"] == trace.trace_id
    assert stored["schema_version"] == 2


def test_full_bundle_failure_marks_response_degraded(monkeypatch):
    # Oracle tools still want a bundle; failure under full mode is degraded.
    monkeypatch.setenv("ASK_FABLE_TRACE_MODE", "full")
    monkeypatch.setattr(trace_runtime.trace_bundle, "write", lambda trace_id, content: None)
    with trace_runtime.tool_trace("ask_codex", {}) as trace:
        payload = trace.complete({"status": "ok"})
    assert payload["status"] == "ok"
    assert payload["telemetry"]["status"] == "degraded"


def test_coordination_tools_skip_bundle_no_false_degraded(monkeypatch, tmp_path):
    """context_list / session_* never write bundles — full mode must not degrade."""
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("ASK_FABLE_TRACE_MODE", "full")
    writes: list[str] = []

    def _capture_write(trace_id, content):
        writes.append(trace_id)
        return None  # would degrade if this tool wanted a bundle

    monkeypatch.setattr(trace_runtime.trace_bundle, "write", _capture_write)
    with trace_runtime.tool_trace("context_list", {}) as trace:
        payload = trace.complete({"status": "ok", "keys": []})
    assert payload["status"] == "ok"
    assert payload["telemetry"]["status"] == "ok"
    assert writes == []  # never attempted
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert not any(e.get("event_name") == "persistence.error" for e in events)


def test_tool_completed_error_carries_kind_and_detail(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    with trace_runtime.tool_trace("ask_ollama", {}) as trace:
        trace.complete(
            {
                "status": "error",
                "kind": "sdk_error",
                "detail": "Ollama request failed",
            }
        )
    terminal = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert terminal["event_name"] == "tool.completed"
    assert terminal["status"] == "error"
    assert terminal["error"] == {
        "category": "error",
        "kind": "sdk_error",
        "detail": "Ollama request failed",
    }


def test_tool_completed_refused_carries_reason_as_detail(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    with trace_runtime.tool_trace("ask", {}) as trace:
        trace.complete(
            {
                "status": "refused",
                "stage": "guard",
                "reason": "offensive-security content",
            }
        )
    terminal = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert terminal["status"] == "refused"
    assert terminal["error"]["category"] == "refused"
    assert terminal["error"]["detail"] == "offensive-security content"
    assert terminal["error"]["stage"] == "guard"


def test_incomplete_no_complete_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    with trace_runtime.tool_trace("ask_codex", {"question": "q"}):
        pass  # exit without complete()
    terminal = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert terminal["status"] == "incomplete"
    assert terminal["error"] == {
        "category": "incomplete",
        "reason": "no_complete",
    }


def test_incomplete_exception_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    try:
        with trace_runtime.tool_trace("ask_codex", {}):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    terminal = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert terminal["status"] == "incomplete"
    assert terminal["error"]["reason"] == "exception"
    assert "RuntimeError: boom" in terminal["error"]["detail"]


def test_incomplete_cancelled_reason(monkeypatch, tmp_path):
    import asyncio

    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    try:
        with trace_runtime.tool_trace("ask_codex", {}):
            raise asyncio.CancelledError()
    except asyncio.CancelledError:
        pass
    terminal = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert terminal["status"] == "incomplete"
    assert terminal["error"]["reason"] == "cancelled"


def test_outer_cache_miss_is_carried_to_terminal_and_response(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    with trace_runtime.tool_trace("ask_codex", {}) as trace:
        trace_runtime.record_stage(
            "cache.outer",
            "miss",
            kind=trace_runtime.EventKind.CACHE,
            cache={"status": "miss", "layer": "outer"},
        )
        payload = trace.complete({"status": "ok"})
    terminal = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert payload["cache"] == {"status": "miss", "layer": "outer"}
    assert terminal["cache"] == payload["cache"]


def test_provider_usage_is_summed_order_independently():
    first = ProviderTelemetry(
        oracle_key="a",
        usage=ProviderUsage(input_tokens=2, output_tokens=3, total_tokens=5, cost_usd=0.1),
    )
    second = ProviderTelemetry(
        oracle_key="b",
        usage=ProviderUsage(input_tokens=7, output_tokens=11, total_tokens=18, cost_usd=0.2),
    )
    with trace_runtime.tool_trace("ask_council", {}) as trace:
        trace.provider(second, "ok")
        trace.provider(first, "ok")
        payload = trace.complete({"status": "ok"})
    assert payload["usage"] == {
        "input_tokens": 9,
        "output_tokens": 14,
        "total_tokens": 23,
        "cost_usd": 0.3,
        "coverage_count": 2,
    }


def test_retry_provider_emits_paired_provider_and_attempt_spans(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    telemetry = ProviderTelemetry(oracle_key="ollama", retry_count=1)
    with trace_runtime.tool_trace("ask_ollama", {}) as trace:
        trace.provider(telemetry, "ok")
        trace.complete({"status": "ok"})
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    provider = [event for event in events if event["event_name"].startswith("provider.")]
    assert [event["event_name"] for event in provider] == [
        "provider.started",
        "provider.completed",
        "provider.attempt.started",
        "provider.attempt.completed",
        "provider.attempt.started",
        "provider.attempt.completed",
    ]
    assert provider[0]["span_id"] == provider[1]["span_id"]
    assert provider[2]["span_id"] == provider[3]["span_id"]


def _events_by_name(tmp_path) -> dict[str, dict]:
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    return {event["event_name"]: event for event in map(json.loads, lines)}


def _stamp(event: dict) -> datetime:
    return datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))


def test_provider_start_events_carry_the_calls_real_start(monkeypatch, tmp_path):
    # provider() runs once the call has returned and stamped its start events then, so a
    # 300 ms call's "start" read ~70 µs before its completion.
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    with trace_runtime.tool_trace("ask_council", {}) as trace:
        trace.started -= 1.0  # the tool has been running for a second already
        trace.provider(ProviderTelemetry(oracle_key="fable", wall_duration_ms=300.0), "ok")
        trace.complete({"status": "ok"})
    events = _events_by_name(tmp_path)
    span = _stamp(events["provider.completed"]) - _stamp(events["provider.started"])
    assert span.total_seconds() == pytest.approx(0.3, abs=0.05)
    first_attempt = events["provider.attempt.started"]  # the only attempt: the call itself
    assert first_attempt["timestamp"] == events["provider.started"]["timestamp"]


def test_provider_start_never_precedes_its_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    with trace_runtime.tool_trace("ask_council", {}) as trace:
        trace.provider(ProviderTelemetry(oracle_key="fable", wall_duration_ms=3.6e6), "ok")
        trace.complete({"status": "ok"})
    events = _events_by_name(tmp_path)
    # clamped to the tool's own start (give or take the tool.started write itself)
    tool_start = _stamp(events["tool.started"])
    assert _stamp(events["provider.started"]) > tool_start - timedelta(seconds=1)


def test_cached_provider_does_not_increment_provider_spans():
    telemetry = ProviderTelemetry(oracle_key="codex", transport="cache")
    with trace_runtime.tool_trace("ask_council", {}) as trace:
        trace_runtime.record_provider(telemetry, "ok")
        payload = trace.complete({"status": "ok"})
    assert payload["telemetry"]["provider_spans"] == 0


def _provider_completed(tmp_path):
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    return [e for e in events if e["event_name"] == "provider.completed"]


def test_provider_records_per_source_answer_digest(monkeypatch, tmp_path):
    """A per-source answer is hashed onto the provider.completed event (answer
    text only, never thinking), so two identical answers hash equal and a changed
    answer differs — the false-consensus / diff-localization signal."""
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    telemetry = ProviderTelemetry(oracle_key="minimax", transport="bridge")
    with trace_runtime.tool_trace("ask_council", {"question": "q"}) as trace:
        trace.provider(telemetry, "ok", thinking="secret reasoning", answer="use a lazy bucket")
        trace.provider(telemetry, "ok", thinking="different reasoning", answer="use a lazy bucket")
        trace.provider(telemetry, "ok", answer="use a background task")
        trace.complete({"status": "ok"})
    completed = _provider_completed(tmp_path)
    d0, d1, d2 = (c["output"] for c in completed[:3])
    # answer-only: identical answers hash equal despite different thinking
    assert d0["sha256"] == d1["sha256"]
    # a changed answer differs
    assert d2["sha256"] != d0["sha256"]
    assert d0["chars"] == len("use a lazy bucket") and d0["redaction_count"] == 0
    # safe mode keeps the digest but not the answer text
    assert "text" not in d0


def test_answer_digest_full_mode_keeps_text_unknown_mode_degrades(monkeypatch, tmp_path):
    """`full` mode keeps the answer text alongside the digest; an unknown trace
    mode (a raw env var the configure_tracing tool never validated) must NOT crash
    the trace write — it degrades to safe (hash only, no text)."""
    telemetry = ProviderTelemetry(oracle_key="minimax", transport="bridge")
    monkeypatch.setenv("ASK_FABLE_TRACE_MODE", "full")
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "full.jsonl"))
    with trace_runtime.tool_trace("ask_council", {"question": "q"}) as trace:
        trace.provider(telemetry, "ok", answer="the answer")
        trace.complete({"status": "ok"})
    events = [json.loads(x) for x in (tmp_path / "full.jsonl").read_text().splitlines()]
    full = [e for e in events if e["event_name"] == "provider.completed"][0]["output"]
    assert full["text"] == "the answer" and full["sha256"]

    monkeypatch.setenv("ASK_FABLE_TRACE_MODE", "verbose")  # not a real mode
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "bad.jsonl"))
    with trace_runtime.tool_trace("ask_council", {"question": "q"}) as trace:
        trace.provider(telemetry, "ok", answer="the answer")  # must not raise
        trace.complete({"status": "ok"})
    events = [json.loads(x) for x in (tmp_path / "bad.jsonl").read_text().splitlines()]
    bad = [e for e in events if e["event_name"] == "provider.completed"][0]["output"]
    assert "text" not in bad and bad["sha256"]  # degraded to safe, no crash


def test_provider_without_answer_has_no_output(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    telemetry = ProviderTelemetry(oracle_key="glm", transport="bridge")
    with trace_runtime.tool_trace("ask_council", {"question": "q"}) as trace:
        trace.provider(telemetry, "ok")  # no answer passed
        trace.complete({"status": "ok"})
    assert _provider_completed(tmp_path)[0]["output"] is None


def test_provider_completed_carries_error_kind(monkeypatch, tmp_path):
    """A failed member inside a council is greppable by kind, and stats can tell
    a backend failure from a call the breaker shed."""
    from ask_fable.provider_telemetry import ProviderTelemetry

    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    with trace_runtime.tool_trace("ask_council", {"question": "q"}) as trace:
        telemetry = ProviderTelemetry(oracle_key="glm", actual_model="GLM-5.2", transport="bridge")
        trace_runtime.record_provider(telemetry, "error", kind="circuit_open")
        trace_runtime.record_provider(telemetry, "ok")
        trace.complete({"status": "ok"})
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    completed = [e for e in events if e["event_name"] == "provider.completed"]
    assert completed[0]["status"] == "error"
    assert completed[0]["error"] == {"category": "error", "kind": "circuit_open"}
    assert completed[1]["status"] == "ok" and completed[1]["error"] is None
