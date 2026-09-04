from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeAlias

from .redaction import redact_text

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
EventBlock: TypeAlias = dict[str, JSONValue]


class CaptureMode(StrEnum):
    SAFE = "safe"
    FULL = "full"


class EventKind(StrEnum):
    ARTIFACT = "artifact"
    CACHE = "cache"
    ERROR = "error"
    INTERNAL = "internal"
    ORCHESTRATION = "orchestration"
    PROVIDER = "provider"
    SERVER = "server"
    SPAN = "span"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ContentCapture:
    sha256: str
    chars: int | None
    bytes: int
    encoding: str
    redaction_count: int
    text: str | None = None

    def to_dict(self) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {
            "sha256": self.sha256,
            "chars": self.chars,
            "bytes": self.bytes,
            "encoding": self.encoding,
            "redaction_count": self.redaction_count,
        }
        if self.text is not None:
            result["text"] = self.text
        return result


def capture_content(value: str | bytes, mode: CaptureMode = CaptureMode.SAFE) -> ContentCapture:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    encoding = "utf-8" if isinstance(value, str) else "binary"
    text = value if isinstance(value, str) else None
    redacted, count = redact_text(text) if text is not None else (None, 0)
    return ContentCapture(
        sha256=hashlib.sha256(raw).hexdigest(),
        chars=len(text) if text is not None else None,
        bytes=len(raw),
        encoding=encoding,
        redaction_count=count,
        text=redacted if mode is CaptureMode.FULL else None,
    )


@dataclass(frozen=True, slots=True)
class TraceEvent:
    schema_version: int
    event_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    event_name: str
    kind: str
    timestamp: str
    duration_ms: float | None
    server_instance_id: str | None
    mcp_request_id: str | None
    project: str | None
    tool: str | None
    mode: str | None
    session: str | None
    status: str | None
    cache: EventBlock | None = None
    provider: EventBlock | None = None
    usage: EventBlock | None = None
    orchestration: EventBlock | None = None
    artifact: EventBlock | None = None
    error: EventBlock | None = None

    @classmethod
    def new(
        cls,
        *,
        event_name: str,
        kind: EventKind | str,
        trace_id: str | None = None,
        span_id: str | None = None,
        project: str | None = None,
        status: str | None = None,
        **fields: str | float | EventBlock | None,
    ) -> TraceEvent:
        return cls(
            schema_version=2,
            event_id=str(uuid.uuid4()),
            trace_id=trace_id or str(uuid.uuid4()),
            span_id=span_id or uuid.uuid4().hex[:16],
            parent_span_id=_string_field(fields, "parent_span_id"),
            event_name=event_name,
            kind=str(kind),
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            duration_ms=_float_field(fields, "duration_ms"),
            server_instance_id=_string_field(fields, "server_instance_id"),
            mcp_request_id=_string_field(fields, "mcp_request_id"),
            project=project,
            tool=_string_field(fields, "tool"),
            mode=_string_field(fields, "mode"),
            session=_string_field(fields, "session"),
            status=status,
            cache=_block_field(fields, "cache"),
            provider=_block_field(fields, "provider"),
            usage=_block_field(fields, "usage"),
            orchestration=_block_field(fields, "orchestration"),
            artifact=_block_field(fields, "artifact"),
            error=_block_field(fields, "error"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, JSONValue]) -> TraceEvent | None:
        if type(value.get("schema_version")) is not int or value["schema_version"] != 2:
            return None
        try:
            required = {
                name: _required_string(value, name)
                for name in ("event_id", "trace_id", "span_id", "event_name")
            }
            timestamp = _utc_timestamp(value)
            kind = EventKind(_required_string(value, "kind"))
            duration = _optional_number(value, "duration_ms")
            optional = {
                name: _optional_string(value, name)
                for name in (
                    "parent_span_id",
                    "server_instance_id",
                    "mcp_request_id",
                    "project",
                    "tool",
                    "mode",
                    "session",
                    "status",
                )
            }
            blocks = {
                name: _optional_block(value, name)
                for name in ("cache", "provider", "usage", "orchestration", "artifact", "error")
            }
            return cls(
                schema_version=2,
                kind=kind.value,
                timestamp=timestamp,
                duration_ms=duration,
                **required,
                **optional,
                **blocks,
            )
        except (KeyError, TypeError, ValueError):
            return None


def _required_string(value: Mapping[str, JSONValue], name: str) -> str:
    item = value[name]
    if not isinstance(item, str) or not item:
        raise TypeError(name)
    return item


def _optional_string(value: Mapping[str, JSONValue], name: str) -> str | None:
    item = value.get(name)
    if item is not None and not isinstance(item, str):
        raise TypeError(name)
    return item


def _optional_number(value: Mapping[str, JSONValue], name: str) -> float | None:
    item = value.get(name)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise TypeError(name)
    number = float(item)
    if not math.isfinite(number):
        raise ValueError(name)
    return number


def _utc_timestamp(value: Mapping[str, JSONValue]) -> str:
    item = _required_string(value, "timestamp")
    parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("timestamp")
    return item


def _optional_block(value: Mapping[str, JSONValue], name: str) -> EventBlock | None:
    item = value.get(name)
    if item is not None and not isinstance(item, dict):
        raise TypeError(name)
    return item


def _string_field(fields: Mapping[str, str | float | EventBlock | None], name: str) -> str | None:
    value = fields.get(name)
    return value if isinstance(value, str) else None


def _float_field(fields: Mapping[str, str | float | EventBlock | None], name: str) -> float | None:
    value = fields.get(name)
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(name)
    return number


def _block_field(fields: Mapping[str, str | float | EventBlock | None], name: str) -> EventBlock | None:
    value = fields.get(name)
    return value if isinstance(value, dict) else None
