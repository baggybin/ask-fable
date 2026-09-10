from __future__ import annotations

import math
from dataclasses import dataclass, field

from .telemetry import EventBlock, JSONValue


def _put(block: EventBlock, name: str, value: JSONValue) -> None:
    if value is not None:
        block[name] = value


def token_count(value: JSONValue) -> int | None:
    return value if type(value) is int and value >= 0 else None


def normalize_cost_usd(value: JSONValue) -> float | None:
    if type(value) not in (int, float):
        return None
    normalized = float(value)
    return normalized if normalized >= 0 and math.isfinite(normalized) else None


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    # What ``cost_usd`` MEANS for this provider — the two are different currencies:
    #   "billed"       real money charged per token (OpenRouter, a direct API key)
    #   "subscription" a LIST price for work covered by a flat plan, so the
    #                  marginal cost is zero (Fable/Opus on Claude Code's OAuth
    #                  session, and the local CLIs on their own plans)
    # Summing the two produces a number that is neither, which is why the
    # aggregate keeps them apart.
    cost_basis: str | None = None

    def to_event_dict(self) -> EventBlock:
        block: EventBlock = {}
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "total_tokens",
            "cost_usd",
        ):
            _put(block, name, getattr(self, name))
        # Only meaningful next to a cost — and it must not make an otherwise
        # empty usage look "available" to usage_if_available().
        if block.get("cost_usd") is not None and self.cost_basis:
            block["cost_basis"] = self.cost_basis
        return block


def usage_if_available(usage: ProviderUsage) -> ProviderUsage | None:
    return usage if usage.to_event_dict() else None


@dataclass(frozen=True, slots=True)
class ToolEvent:
    name: str
    call_id: str | None = None
    status: str | None = None
    duration_ms: float | None = None
    exit_code: int | None = None
    input_fingerprint: str | None = None
    output_fingerprint: str | None = None

    def to_event_dict(self) -> EventBlock:
        block: EventBlock = {"name": self.name}
        for name in (
            "call_id",
            "status",
            "duration_ms",
            "exit_code",
            "input_fingerprint",
            "output_fingerprint",
        ):
            _put(block, name, getattr(self, name))
        return block


@dataclass(frozen=True, slots=True)
class ProviderTelemetry:
    oracle_key: str
    requested_model: str | None = None
    actual_model: str | None = None
    transport: str | None = None
    provider_request_id: str | None = None
    provider_session_id: str | None = None
    stop_reason: str | None = None
    returncode: int | None = None
    http_status: int | None = None
    retry_count: int = 0
    wall_duration_ms: float | None = None
    provider_duration_ms: float | None = None
    api_duration_ms: float | None = None
    reasoning_available: bool | None = None
    usage_available: bool | None = None
    tools_available: bool | None = None
    unknown_event_count: int = 0
    unknown_event_fingerprints: tuple[str, ...] = ()
    usage: ProviderUsage | None = None
    tool_events: tuple[ToolEvent, ...] = field(default_factory=tuple)

    def to_event_dict(self) -> EventBlock:
        block: EventBlock = {
            "oracle_key": self.oracle_key,
            "retry_count": self.retry_count,
            "unknown_event_count": self.unknown_event_count,
            "tool_events": [event.to_event_dict() for event in self.tool_events],
        }
        for name in (
            "requested_model",
            "actual_model",
            "transport",
            "provider_request_id",
            "provider_session_id",
            "stop_reason",
            "returncode",
            "http_status",
            "wall_duration_ms",
            "provider_duration_ms",
            "api_duration_ms",
            "reasoning_available",
            "usage_available",
            "tools_available",
        ):
            _put(block, name, getattr(self, name))
        if self.unknown_event_fingerprints:
            block["unknown_event_fingerprints"] = list(self.unknown_event_fingerprints)
        if self.usage is not None:
            block["usage"] = self.usage.to_event_dict()
        return block
