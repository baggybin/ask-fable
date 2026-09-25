import math

import pytest

from ask_fable.provider_telemetry import (
    ProviderTelemetry,
    ProviderUsage,
    ToolEvent,
    normalize_cost_usd,
    usage_if_available,
)


def test_event_dict_when_optional_provider_fields_are_present() -> None:
    # Given
    telemetry = ProviderTelemetry(
        oracle_key="codex",
        requested_model="requested",
        actual_model="actual",
        usage=ProviderUsage(input_tokens=10, output_tokens=4, total_tokens=14),
        tool_events=(ToolEvent(name="command", call_id="call-1", status="completed"),),
    )

    # When
    event = telemetry.to_event_dict()

    # Then
    assert event["actual_model"] == "actual"
    assert event["usage"] == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
    assert event["tool_events"] == [{"name": "command", "call_id": "call-1", "status": "completed"}]


def test_event_dict_when_optional_provider_fields_are_absent() -> None:
    # Given
    telemetry = ProviderTelemetry(oracle_key="gemini", usage_available=False, tools_available=False)

    # When
    event = telemetry.to_event_dict()

    # Then
    assert event == {
        "oracle_key": "gemini",
        "retry_count": 0,
        "unknown_event_count": 0,
        "tool_events": [],
        "usage_available": False,
        "tools_available": False,
    }


@pytest.mark.parametrize("value", [True, "1.25", -1, math.nan, math.inf, -math.inf])
def test_cost_normalizes_to_none_when_value_is_not_finite_nonnegative_number(value) -> None:
    assert normalize_cost_usd(value) is None


def test_usage_is_unavailable_when_every_normalized_field_is_none() -> None:
    usage = ProviderUsage(input_tokens=None, cost_usd=None)

    assert usage_if_available(usage) is None


def test_cost_preserves_finite_nonnegative_number() -> None:
    assert normalize_cost_usd(0) == 0.0
    assert normalize_cost_usd(1.25) == 1.25
