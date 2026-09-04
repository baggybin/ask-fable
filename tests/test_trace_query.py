from __future__ import annotations

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
