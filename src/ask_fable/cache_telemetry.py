from __future__ import annotations


def prepare_cache_hit(payload: dict, *, age_seconds: int) -> dict:
    source_trace_id = payload.get("origin_trace_id") or payload.get("trace_id")
    stale = {
        "trace_id", "origin_trace_id", "duration_ms", "saved", "artifacts", "telemetry",
        "cache", "resolved_context_refs", "missing_context_refs",
    }
    served = {key: value for key, value in payload.items() if key not in stale}
    served["cached"] = True
    served["cache_age_s"] = age_seconds
    served["cache"] = {
        "status": "hit", "layer": "outer", "age_ms": age_seconds * 1000,
        "source_trace_id": source_trace_id if isinstance(source_trace_id, str) else None,
    }
    return served


def prepare_cache_store(payload: dict) -> dict:
    from .trace_runtime import current

    stored = dict(payload)
    trace = current()
    stored["schema_version"] = 2
    stored["origin_trace_id"] = trace.trace_id if trace is not None else None
    return stored
