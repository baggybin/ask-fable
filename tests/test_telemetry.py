from __future__ import annotations

import json
import math

from ask_fable.telemetry import CaptureMode, EventKind, TraceEvent, capture_content, redact_text


def test_schema_v2_round_trip_tolerates_unknown_optional_fields() -> None:
    # Given
    event = TraceEvent.new(event_name="tool.complete", kind="tool", project="demo")
    payload = event.to_dict() | {"future_field": {"enabled": True}}

    # When
    parsed = TraceEvent.from_dict(payload)

    # Then
    assert parsed is not None
    assert parsed.schema_version == 2
    assert parsed.event_name == "tool.complete"


def test_safe_capture_never_contains_canary() -> None:
    # Given
    canary = "CANARY-secret-token"

    # When
    captured = capture_content(canary, CaptureMode.SAFE)

    # Then
    encoded = json.dumps(captured.to_dict())
    assert canary not in encoded
    assert captured.sha256
    assert captured.chars == len(canary)


def test_full_capture_redacts_secrets_before_serialization() -> None:
    # Given
    source = "Authorization: Bearer super-secret\napi_key=sk-live-canary"

    # When
    captured = capture_content(source, CaptureMode.FULL)

    # Then
    encoded = json.dumps(captured.to_dict())
    assert "super-secret" not in encoded
    assert "sk-live-canary" not in encoded
    assert captured.redaction_count == 2
    assert captured.text is not None


def test_full_capture_consumes_quoted_and_space_bearing_secrets() -> None:
    # Given
    source = 'password="two word canary"\ntoken: alpha beta canary\nAuthorization: Basic xyz canary'

    # When
    captured = capture_content(source, CaptureMode.FULL)

    # Then
    assert captured.text == "password=[REDACTED]\ntoken: [REDACTED]\nAuthorization: [REDACTED]"
    assert "canary" not in json.dumps(captured.to_dict())
    assert captured.redaction_count == 3


def test_binary_capture_is_well_formed_and_non_leaking() -> None:
    # Given
    source = b"\xff\x00secret"

    # When
    captured = capture_content(source, CaptureMode.SAFE)

    # Then
    assert captured.bytes == len(source)
    assert captured.encoding == "binary"
    assert captured.text is None


def test_full_capture_redacts_uri_cookie_and_jwt() -> None:
    source = (
        "postgres://alice:password@db.example/app\n"
        "Cookie: session=private-cookie\n"
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.signaturevalue"
    )
    captured = capture_content(source, CaptureMode.FULL)
    assert "password" not in captured.text
    assert "private-cookie" not in captured.text
    assert "eyJhbGci" not in captured.text


def test_redaction_handles_malformed_secret_lines() -> None:
    # Given / When
    redacted, count = redact_text("token =\npassword: hunter2")

    # Then
    assert "hunter2" not in redacted
    assert count == 1


def test_schema_v2_parser_rejects_missing_ill_typed_and_non_integer_version() -> None:
    # Given
    valid = TraceEvent.new(event_name="tool.complete", kind=EventKind.TOOL).to_dict()

    # When
    missing = TraceEvent.from_dict({key: value for key, value in valid.items() if key != "event_id"})
    ill_typed = TraceEvent.from_dict(valid | {"duration_ms": "fast"})
    float_version = TraceEvent.from_dict(valid | {"schema_version": 2.0})
    unknown_kind = TraceEvent.from_dict(valid | {"kind": "surprise"})

    # Then
    assert (missing, ill_typed, float_version, unknown_kind) == (None, None, None, None)


def test_event_rejects_non_finite_duration_in_constructor_and_parser() -> None:
    # Given
    valid = TraceEvent.new(event_name="tool.complete", kind=EventKind.TOOL).to_dict()

    # When / Then
    for duration in (math.nan, math.inf, -math.inf):
        try:
            TraceEvent.new(event_name="tool.complete", kind=EventKind.TOOL, duration_ms=duration)
        except ValueError:
            pass
        else:
            raise AssertionError("non-finite constructor duration was accepted")
        assert TraceEvent.from_dict(valid | {"duration_ms": duration}) is None


def test_schema_v2_parser_requires_timezone_aware_utc_timestamp() -> None:
    # Given
    valid = TraceEvent.new(event_name="tool.complete", kind=EventKind.TOOL).to_dict()

    # When / Then
    assert TraceEvent.from_dict(valid | {"timestamp": "not-a-date"}) is None
    assert TraceEvent.from_dict(valid | {"timestamp": "2026-01-01T00:00:00"}) is None
    assert TraceEvent.from_dict(valid | {"timestamp": "2026-01-01T01:00:00+01:00"}) is None
    assert TraceEvent.from_dict(valid | {"timestamp": "2026-01-01T00:00:00Z"}) is not None
