from __future__ import annotations

import json

from ask_fable.telemetry import EventKind, TraceEvent
from ask_fable.trace_query import get_trace, list_traces
from ask_fable.trace_store import EventStore


def test_list_traces_returns_newest_summary(tmp_path):
    path = tmp_path / "events.jsonl"
    store = EventStore(path=path)
    store.append(TraceEvent.new(event_name="tool.started", kind=EventKind.TOOL, trace_id="a", tool="ask"))
    store.append(TraceEvent.new(event_name="tool.completed", kind=EventKind.TOOL, trace_id="a", tool="ask", status="ok"))
    result = list_traces(path, limit=20)
    assert result["status"] == "ok"
    assert result["traces"][0]["trace_id"] == "a"
    assert result["traces"][0]["status"] == "ok"


def test_get_trace_returns_ordered_events_without_content(tmp_path):
    path = tmp_path / "events.jsonl"
    store = EventStore(path=path)
    store.append(TraceEvent.new(event_name="tool.started", kind=EventKind.TOOL, trace_id="a", tool="ask"))
    result = get_trace(path, "a")
    assert result["status"] == "ok"
    assert [event["event_name"] for event in result["events"]] == ["tool.started"]


def test_get_trace_rejects_missing_identifier(tmp_path):
    result = get_trace(tmp_path / "events.jsonl", "")
    assert result == {"status": "error", "kind": "bad_args", "detail": "`trace_id` is required"}


def test_get_trace_returns_capped_full_content(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    store = EventStore(path=path)
    store.append(TraceEvent.new(event_name="tool.completed", kind=EventKind.TOOL, trace_id="a"))
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    (trace_dir / "a.json").write_text('{"content":"abcdefghijklmnopqrstuvwxyz"}')
    monkeypatch.setenv("ASK_FABLE_TRACE_DIR", str(trace_dir))
    result = get_trace(path, "a", include_content=True, max_chars=10)
    assert result["content"] == '{"content"'
    assert result["content_truncated"] is True


_OLD = "2026-01-01T00:00:00.000001Z"


def _line(trace_id: str, event_name: str = "tool.completed", **fields) -> str:
    """One stored event, stamped long ago."""
    kind = EventKind.PROVIDER if event_name.startswith("provider.") else EventKind.TOOL
    event = TraceEvent.new(
        event_name=event_name, kind=kind, trace_id=trace_id, tool="ask", **fields
    )
    return json.dumps({**event.to_dict(), "timestamp": _OLD}) + "\n"


def _append_new_trace(path) -> None:
    event = TraceEvent.new(
        event_name="tool.completed", kind=EventKind.TOOL, trace_id="new", tool="ask", status="ok"
    )
    assert EventStore(path=path).append(event)


def test_new_trace_is_found_after_the_log_outgrows_the_scan_cap(monkeypatch, tmp_path):
    # The scan read oldest-first and stopped at its cap, so once the log outgrew it (one
    # rotated 50 MB segment was enough) no new trace was ever listed or found again.
    monkeypatch.setenv("ASK_FABLE_TRACE_QUERY_MAX_EVENTS", "20")
    segment = tmp_path / "events.20260101T000000000000Z.000000.jsonl"
    segment.write_text("".join(_line(f"old-{n}") for n in range(30)))
    path = tmp_path / "events.jsonl"
    _append_new_trace(path)
    assert get_trace(path, "new")["status"] == "ok"
    assert list_traces(path, limit=1)["traces"][0]["trace_id"] == "new"


def test_scan_cap_drops_the_oldest_events_of_the_active_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_TRACE_QUERY_MAX_EVENTS", "5")
    path = tmp_path / "events.jsonl"
    path.write_text("".join(_line(f"old-{n}") for n in range(10)))
    _append_new_trace(path)
    assert get_trace(path, "new")["status"] == "ok"
    assert get_trace(path, "old-0")["kind"] == "not_found"  # the oldest fell off instead


def test_newest_first_scan_keeps_event_order_and_summary_fields(tmp_path):
    # Same-timestamp events still come back in write order, an in-flight trace is still
    # named by its first event (provider events carry no session), and providers keep
    # first-appearance order.
    path = tmp_path / "events.jsonl"
    path.write_text(
        _line("a", "tool.started", session="s", status="started")
        + _line("a", "provider.completed", provider={"actual_model": "m1"})
        + _line("a", "provider.completed", provider={"actual_model": "m2"})
        + _line("a", "provider.completed", provider={"actual_model": "m2"}, status="error")
    )
    events = get_trace(path, "a")["events"]
    assert [e["event_name"] for e in events] == ["tool.started"] + ["provider.completed"] * 3
    assert events[-1]["status"] == "error"
    (summary,) = list_traces(path, session="s")["traces"]
    assert summary["providers"] == ["m1", "m2"] and summary["status"] == "incomplete"
