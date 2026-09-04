from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from . import audit, trace_bundle
from .cache_telemetry import prepare_cache_hit as prepare_cache_hit
from .cache_telemetry import prepare_cache_store as prepare_cache_store
from .provider_telemetry import ProviderTelemetry
from .telemetry import EventKind, JSONValue, TraceEvent
from .trace_hooks import record_event as record_event
from .trace_hooks import record_provider as record_provider
from .trace_hooks import record_stage as record_stage
from .trace_hooks import set_orchestration as set_orchestration
from .trace_store import EventStore

# Tools that are coordination / meta only — no oracle call, no need for a full
# arguments+response bundle on disk. Skipping them avoids false
# ``trace_bundle_write`` degraded events under TRACE_MODE=full.
_NO_BUNDLE_TOOLS = frozenset(
    {
        "trace_list",
        "trace_get",
        "stats",
        "session_list",
        "session_peek",
        "session_stats",
        "context_list",
        "context_read",
        "context_delete",
        "list_ollama_models",
        "list_atlas_models",
        "configure_tracing",
        "configure_ollama_council",
        "reset_session",
    }
)


def _project_fingerprint() -> str:
    explicit = os.environ.get("ASK_FABLE_PROJECT_ID")
    if explicit:
        return explicit
    return hashlib.sha256(os.getcwd().encode()).hexdigest()[:16]


def project_fingerprint() -> str:
    """Public alias for ``_project_fingerprint`` (kept private for back-compat).
    Stable 16-char project id: ``ASK_FABLE_PROJECT_ID`` if set, else a sha256
    prefix of the cwd. Reused by the hub for cross-project session scoping."""
    return _project_fingerprint()


def _store() -> EventStore:
    return audit.event_store()


def _error_from_payload(payload: dict, status: str) -> dict[str, JSONValue] | None:
    """Build a greppable error block for tool.completed from the response payload.

    Oracle / validation failures already carry ``kind`` + ``detail`` (or
    ``reason``) in the JSON returned to the client; historically only a sha256 of
    that payload landed in the audit log. Surface them on the event so
    ``rg kind decisions.jsonl`` works without opening a trace bundle.
    """
    if status not in {"error", "refused", "denied"}:
        return None
    block: dict[str, JSONValue] = {"category": status}
    kind = payload.get("kind")
    if isinstance(kind, str) and kind.strip():
        block["kind"] = kind.strip()
    detail = payload.get("detail")
    if not (isinstance(detail, str) and detail.strip()):
        reason = payload.get("reason")
        detail = reason if isinstance(reason, str) else None
    if isinstance(detail, str) and detail.strip():
        text = detail.strip()
        block["detail"] = text if len(text) <= 500 else text[:499] + "…"
    stage = payload.get("stage")
    if isinstance(stage, str) and stage.strip():
        block["stage"] = stage.strip()
    # Always emit a block for error/refused/denied so status alone isn't the
    # only signal (even when kind/detail were omitted).
    return block


@dataclass(slots=True)
class ToolTrace:
    tool: str
    arguments: dict[str, JSONValue] = field(default_factory=dict)
    mcp_request_id: str | None = None
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    started: float = field(default_factory=time.monotonic)
    degraded: bool = False
    completed: bool = False
    provider_spans: int = 0
    tool_events: int = 0
    reasoning_available: bool = False
    usage: dict[str, JSONValue] | None = None
    cache_summary: dict[str, JSONValue] | None = None
    reasoning: list[dict[str, JSONValue]] = field(default_factory=list)
    orchestration: dict[str, JSONValue] | None = None

    @property
    def session(self) -> str | None:
        value = self.arguments.get("session")
        return value if isinstance(value, str) else None

    @property
    def mode(self) -> str:
        from . import config  # local import avoids any import-cycle at module load

        return (config.setting("ASK_FABLE_TRACE_MODE") or "safe").strip().lower()

    def emit(
        self,
        event_name: str,
        kind: EventKind,
        status: str,
        *,
        child: bool = False,
        **fields: str | float | dict[str, JSONValue] | None,
    ) -> None:
        """Write one event on this tool's trace. The tool's own lifecycle events
        ride the tool span itself; ``child=True`` mints a fresh span parented to
        it — what provider and stage spans do — so a tool span id keeps
        identifying exactly one span."""
        if child:
            fields.setdefault("parent_span_id", self.span_id)
        event = TraceEvent.new(
            event_name=event_name,
            kind=kind,
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:16] if child else self.span_id,
            project=_project_fingerprint(),
            status=status,
            tool=self.tool,
            mcp_request_id=self.mcp_request_id,
            server_instance_id=_SERVER_INSTANCE_ID,
            session=self.session,
            mode=self.mode,
            **fields,
        )
        if not _store().append(event):
            self.degraded = True

    def complete(self, payload: dict) -> dict:
        duration_ms = int((time.monotonic() - self.started) * 1000)
        status = str(payload.get("status") or "ok")
        effective_cache = (
            payload.get("cache") if isinstance(payload.get("cache"), dict) else self.cache_summary
        )
        saved = payload.get("saved")
        artifacts = list(payload.get("artifacts") or [])
        # Coordination / meta tools: no oracle payload worth bundling; skip write
        # entirely so full mode does not emit false trace_bundle_write degraded.
        wants_bundle = self.tool not in _NO_BUNDLE_TOOLS
        bundle = None
        if wants_bundle:
            bundle = trace_bundle.write(
                self.trace_id,
                {
                    "arguments": self.arguments,
                    "response": payload,
                    "provider_reasoning": self.reasoning,
                },
            )
        if bundle is not None:
            bundle_artifact: dict[str, JSONValue] = {"kind": "trace_bundle", "path": str(bundle)}
            artifacts.append(bundle_artifact)
            self.emit("artifact.saved", EventKind.ARTIFACT, "ok", artifact=bundle_artifact)
        elif wants_bundle and self.mode == "full":
            self.degraded = True
            self.emit(
                "persistence.error",
                EventKind.ERROR,
                "degraded",
                error={"category": "trace_bundle_write"},
            )
        if isinstance(saved, str):
            artifact: dict[str, JSONValue] = {"kind": "markdown", "path": saved}
            artifacts.append(artifact)
            self.emit("artifact.saved", EventKind.ARTIFACT, "ok", artifact=artifact)
        if not self.completed:
            output_capture = json.dumps(payload, ensure_ascii=False, default=str)
            error_block = _error_from_payload(payload, status)
            orchestration = dict(self.orchestration or {})
            orchestration.update(
                {
                    "output": {
                        "sha256": hashlib.sha256(output_capture.encode()).hexdigest(),
                        "chars": len(output_capture),
                        "bytes": len(output_capture.encode()),
                    },
                    "model": next(
                        (
                            value
                            for value in (
                                payload.get("model"),
                                payload.get("answered_by"),
                                payload.get("synthesizer"),
                            )
                            if isinstance(value, str) and value
                        ),
                        None,
                    ),
                }
            )
            self.emit(
                "tool.completed",
                EventKind.TOOL,
                status,
                duration_ms=duration_ms,
                cache=effective_cache,
                usage=(
                    payload.get("usage") if isinstance(payload.get("usage"), dict) else self.usage
                ),
                artifact={"items": artifacts} if artifacts else None,
                error=error_block,
                orchestration=orchestration,
            )
            self.completed = True
        result = dict(payload)
        result.update(
            {
                "schema_version": 2,
                "trace_id": self.trace_id,
                "duration_ms": duration_ms,
                "cache": effective_cache,
                "usage": result.get("usage") or self.usage,
                "artifacts": artifacts,
                "telemetry": {
                    "status": "degraded" if self.degraded else "ok",
                    "provider_spans": self.provider_spans,
                    "tool_events": self.tool_events,
                    "reasoning_available": self.reasoning_available,
                },
            }
        )
        return result

    def provider(
        self,
        telemetry: ProviderTelemetry,
        status: str,
        thinking: str = "",
        *,
        kind: str = "",
    ) -> None:
        usage = telemetry.usage.to_event_dict() if telemetry.usage is not None else None
        provider_span_id = uuid.uuid4().hex[:16]
        # Same shape as tool.completed's error block, so a member that failed
        # inside a council is greppable by kind — and so stats can tell a real
        # backend failure from a call the breaker shed (``circuit_open``).
        error: dict[str, JSONValue] | None = None
        if status in {"error", "refused"}:
            error = {"category": status}
            if kind:
                error["kind"] = kind
        started_event = TraceEvent.new(
            event_name="provider.started",
            kind=EventKind.PROVIDER,
            trace_id=self.trace_id,
            span_id=provider_span_id,
            project=_project_fingerprint(),
            status="started",
            tool=self.tool,
            parent_span_id=self.span_id,
            provider=telemetry.to_event_dict(),
        )
        completed_event = TraceEvent.new(
            event_name="provider.completed",
            kind=EventKind.PROVIDER,
            trace_id=self.trace_id,
            span_id=provider_span_id,
            project=_project_fingerprint(),
            status=status,
            tool=self.tool,
            parent_span_id=self.span_id,
            duration_ms=telemetry.wall_duration_ms,
            provider=telemetry.to_event_dict(),
            usage=usage,
            error=error,
        )
        if not _store().append(started_event) or not _store().append(completed_event):
            self.degraded = True
        for attempt in range(1, telemetry.retry_count + 2):
            attempt_span_id = uuid.uuid4().hex[:16]
            attempt_status = status if attempt == telemetry.retry_count + 1 else "retry"
            for event_name, event_status in (
                ("provider.attempt.started", "started"),
                ("provider.attempt.completed", attempt_status),
            ):
                event = TraceEvent.new(
                    event_name=event_name,
                    kind=EventKind.SPAN,
                    trace_id=self.trace_id,
                    span_id=attempt_span_id,
                    project=_project_fingerprint(),
                    status=event_status,
                    tool=self.tool,
                    parent_span_id=provider_span_id,
                    orchestration={"attempt": attempt},
                )
                if not _store().append(event):
                    self.degraded = True
        self.provider_spans += 1
        self.tool_events += len(telemetry.tool_events)
        self.reasoning_available = self.reasoning_available or bool(thinking)
        if thinking:
            self.reasoning.append({"provider": telemetry.oracle_key, "thinking": thinking})
        if usage:
            if self.usage is None:
                self.usage = {"coverage_count": 0}
            self.usage["coverage_count"] = int(self.usage["coverage_count"] or 0) + 1
            basis = usage.get("cost_basis")
            for name, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if name == "cost_usd" and basis == "subscription":
                        # A flat-plan oracle reports a list price, not money
                        # spent. Adding it to per-token spend gives a total that
                        # is neither real cost nor list price — a council mixing
                        # Fable with a billed gateway used to report exactly that.
                        name = "cost_usd_notional"
                    prior = self.usage.get(name)
                    total = (prior if isinstance(prior, (int, float)) else 0) + value
                    self.usage[name] = (
                        round(total, 12) if name.startswith("cost_usd") else total
                    )

    def stage(
        self,
        name: str,
        status: str,
        *,
        kind: EventKind = EventKind.SPAN,
        cache: dict[str, JSONValue] | None = None,
        orchestration: dict[str, JSONValue] | None = None,
    ) -> None:
        span_id = uuid.uuid4().hex[:16]
        for event_name, event_status in (
            (f"{name}.started", "started"),
            (f"{name}.completed", status),
        ):
            event = TraceEvent.new(
                event_name=event_name,
                kind=kind,
                trace_id=self.trace_id,
                span_id=span_id,
                project=_project_fingerprint(),
                status=event_status,
                tool=self.tool,
                parent_span_id=self.span_id,
                cache=cache,
                orchestration=orchestration,
            )
            if not _store().append(event):
                self.degraded = True
        if name == "cache.outer" and cache is not None:
            self.cache_summary = dict(cache)

    def set_orchestration(self, **kwargs: JSONValue) -> None:
        if self.orchestration is None:
            self.orchestration = {}
        self.orchestration.update(kwargs)

    def persistence_failure(self, category: str) -> None:
        self.degraded = True
        self.emit(
            "persistence.error",
            EventKind.ERROR,
            "degraded",
            error={"category": category},
        )


_ACTIVE_TRACE: ContextVar[ToolTrace | None] = ContextVar("ask_fable_trace", default=None)


@contextmanager
def tool_trace(
    tool: str,
    _arguments: dict,
    *,
    mcp_request_id: str | None = None,
) -> Iterator[ToolTrace]:
    """Open a per-call trace scope. Emits ``tool.started`` / ``tool.completed``.

    If the body exits without ``trace.complete()``, a terminal
    ``tool.completed`` with ``status=incomplete`` is written. The ``error``
    block carries a reason:

    - ``cancelled`` — ``asyncio.CancelledError`` or generator close (client abort)
    - ``exception`` — uncaught exception (``detail`` = ``Type: message``)
    - ``no_complete`` — normal exit that never called ``complete()`` (bug/test)
    """
    trace = ToolTrace(tool=tool, arguments=dict(_arguments), mcp_request_id=mcp_request_id)
    token = _ACTIVE_TRACE.set(trace)
    argument_text = json.dumps(_arguments, ensure_ascii=False, default=str)
    trace.emit(
        "tool.started",
        EventKind.TOOL,
        "started",
        orchestration={
            "arguments": {
                "sha256": hashlib.sha256(argument_text.encode()).hexdigest(),
                "chars": len(argument_text),
                "bytes": len(argument_text.encode()),
            }
        },
    )
    incomplete_reason = "no_complete"
    incomplete_detail: str | None = None
    try:
        yield trace
    except asyncio.CancelledError:
        incomplete_reason = "cancelled"
        raise
    except GeneratorExit:
        # Context closed without finishing (client disconnect / task teardown).
        incomplete_reason = "cancelled"
        raise
    except Exception as exc:
        incomplete_reason = "exception"
        incomplete_detail = f"{type(exc).__name__}: {exc}"
        if len(incomplete_detail) > 300:
            incomplete_detail = incomplete_detail[:299] + "…"
        raise
    finally:
        if not trace.completed:
            err: dict[str, JSONValue] = {
                "category": "incomplete",
                "reason": incomplete_reason,
            }
            if incomplete_detail:
                err["detail"] = incomplete_detail
            trace.emit(
                "tool.completed",
                EventKind.TOOL,
                "incomplete",
                duration_ms=int((time.monotonic() - trace.started) * 1000),
                error=err,
            )
        _ACTIVE_TRACE.reset(token)


def current() -> ToolTrace | None:
    return _ACTIVE_TRACE.get()


_SERVER_INSTANCE_ID = str(uuid.uuid4())
