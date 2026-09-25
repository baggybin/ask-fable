from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from . import trace_bundle
from .telemetry import JSONValue, TraceEvent
from .trace_store import EventStore


def _max_events() -> int:
    try:
        return max(1, int(os.environ.get("ASK_FABLE_TRACE_QUERY_MAX_EVENTS") or 100_000))
    except ValueError:
        return 100_000


def _max_bytes() -> int:
    try:
        return max(1, int(os.environ.get("ASK_FABLE_TRACE_QUERY_MAX_BYTES") or 52_428_800))
    except ValueError:
        return 52_428_800


def list_traces(
    path: Path,
    *,
    limit: int = 20,
    tool: str | None = None,
    status: str | None = None,
    provider: str | None = None,
    session: str | None = None,
    project: str | None = None,
    before: str | None = None,
) -> dict[str, JSONValue]:
    summaries: dict[str, dict[str, JSONValue]] = {}
    completed: set[str] = set()
    scanned_bytes = 0
    # Newest first, so the scan caps drop the OLDEST events. Read oldest-first, a log past
    # the caps (one rotated 50 MB segment) hid every newer trace — for good, as rotated
    # segments are kept.
    for index, record in enumerate(EventStore(path=path).iter_records(newest_first=True)):
        scanned_bytes += len(json.dumps(record.payload, ensure_ascii=False).encode())
        if scanned_bytes > _max_bytes():
            break
        if index >= _max_events():
            break
        if record.schema_version != 2:
            continue
        event = TraceEvent.from_dict(record.payload)
        if event is None or (tool and event.tool != tool):
            continue
        summary = summaries.setdefault(
            event.trace_id,
            {
                "trace_id": event.trace_id,
                "timestamp": event.timestamp,
                "tool": event.tool,
                "status": "incomplete",
                "duration_ms": None,
                "session": event.session,
                "project": event.project,
                "providers": [],
            },
        )
        # Each event is older than every one already seen for its trace, so the trace's
        # earliest event still names it and `providers` keeps first-appearance order.
        summary.update(tool=event.tool, session=event.session, project=event.project)
        if event.provider:
            model = event.provider.get("actual_model") or event.provider.get("requested_model")
            if isinstance(model, str):
                if model in summary["providers"]:
                    summary["providers"].remove(model)
                summary["providers"].insert(0, model)
        if event.timestamp > str(summary["timestamp"]):
            summary["timestamp"] = event.timestamp
        if event.event_name == "tool.completed" and event.trace_id not in completed:
            completed.add(event.trace_id)  # the newest completion wins, as before
            summary["status"] = event.status
            summary["duration_ms"] = event.duration_ms
    values = [
        item for item in summaries.values()
        if (not status or item["status"] == status)
        and (not provider or provider in item["providers"])
        and (not session or item["session"] == session)
        and (not project or item["project"] == project)
    ]
    values.sort(key=lambda item: str(item["timestamp"]), reverse=True)
    if before:
        values = [item for item in values if str(item["timestamp"]) < before]
    bounded = max(1, min(limit, 100))
    return {
        "status": "ok",
        "traces": values[:bounded],
        "next_before": values[bounded - 1]["timestamp"] if len(values) > bounded else None,
    }


def get_trace(
    path: Path,
    trace_id: str,
    *,
    include_content: bool = False,
    max_chars: int = 4000,
) -> dict[str, JSONValue]:
    if not trace_id:
        return {"status": "error", "kind": "bad_args", "detail": "`trace_id` is required"}
    events: list[Mapping[str, JSONValue]] = []
    scanned_bytes = 0
    # Newest first so the scan caps drop the OLDEST events (see list_traces).
    for index, record in enumerate(EventStore(path=path).iter_records(newest_first=True)):
        scanned_bytes += len(json.dumps(record.payload, ensure_ascii=False).encode())
        if scanned_bytes > _max_bytes():
            break
        if index >= _max_events():
            break
        if record.schema_version == 2 and record.payload.get("trace_id") == trace_id:
            events.append(record.payload)
    events.reverse()  # back to write order, which the stable sort keeps for equal timestamps
    events.sort(key=lambda event: str(event.get("timestamp") or ""))
    if not events:
        return {"status": "error", "kind": "not_found", "detail": "trace not found"}
    result: dict[str, JSONValue] = {
        "status": "ok",
        "requested_trace_id": trace_id,
        "events": [dict(event) for event in events],
        "artifacts": [event["artifact"] for event in events if event.get("artifact")],
    }
    if include_content:
        content = trace_bundle.read(trace_id, max_chars)
        if content is not None:
            result["content"], result["content_truncated"] = content
    return result
