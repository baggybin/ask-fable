from __future__ import annotations

from .provider_telemetry import ProviderTelemetry
from .telemetry import EventKind, JSONValue


def record_provider(
    telemetry: ProviderTelemetry | None,
    status: str,
    thinking: str = "",
    *,
    kind: str = "",
) -> None:
    from .trace_runtime import current

    trace = current()
    if trace is not None and telemetry is not None and telemetry.transport != "cache":
        trace.provider(telemetry, status, thinking, kind=kind)


def record_event(
    event_name: str,
    kind: EventKind,
    status: str,
    **fields: str | float | dict[str, JSONValue] | None,
) -> None:
    """One instantaneous event nested under the active tool span — no
    started/completed pair — for a state change that is not a stage, such as a
    breaker transition."""
    from .trace_runtime import current

    trace = current()
    if trace is not None:
        trace.emit(event_name, kind, status, child=True, **fields)


def record_stage(
    name: str,
    status: str,
    *,
    kind: EventKind = EventKind.SPAN,
    cache: dict[str, JSONValue] | None = None,
    orchestration: dict[str, JSONValue] | None = None,
) -> None:
    from .trace_runtime import current

    trace = current()
    if trace is not None:
        trace.stage(name, status, kind=kind, cache=cache, orchestration=orchestration)


def set_orchestration(**kwargs: JSONValue) -> None:
    from .trace_runtime import current

    trace = current()
    if trace is not None:
        trace.set_orchestration(**kwargs)
