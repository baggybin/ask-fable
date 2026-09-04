"""Aggregate the audit JSONL into operator-facing usage/health stats.

The audit log already records every call (``decision``, ``model``, ``session``,
``duration_ms``, ``reason``, and — since Theme H — ``quorum``/``consensus``/
``synth_fallback`` for councils and chains), but nothing read it back. This module
streams the log (plus any rotation generations ``.1``/``.2``/``.3``) and
buckets it, so the ``stats`` tool can answer "is GLM erroring 40% of the time?" or
"have my councils been degraded all day?" without the operator spelunking JSONL.

Pure and read-only: no state, no writes, malformed lines are skipped, a missing
log yields empty buckets rather than an error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .trace_store import EventStore

WINDOWS = ("1h", "24h", "7d", "all")
_WINDOW_S = {"1h": 3600.0, "24h": 86400.0, "7d": 604800.0}
BY = ("model", "session", "day", "tool", "provider", "project", "cache", "mode")


def _parse_ts(raw: str) -> float | None:
    """Epoch seconds from the audit record's ISO-Z timestamp, or None."""
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError):
        return None


def _iter_records(path: Path):
    """Yield parsed records from the log plus rotation files, skipping junk.

    Rotations are read oldest-first (``.3`` → ``.1`` → current) purely for
    tidiness — aggregation is order-independent."""
    yield from (dict(record.payload) for record in EventStore(path=path).iter_records())


def _bucket_key(rec: dict, by: str) -> str:
    if by == "mode":
        # aggregate() already normalized orchestration.mode into rec["mode"];
        # reading orchestration directly here would turn single-model calls
        # (orchestration present, no "mode" key) into a bucket named "None".
        return str(rec.get("mode") or "?")
    if by in ("tool", "project"):
        return str(rec.get(by) or "?")
    if by == "provider":
        provider = rec.get("provider")
        return (
            str(provider.get("actual_model") or provider.get("requested_model") or "?")
            if isinstance(provider, dict)
            else "?"
        )
    if by == "cache":
        cache = rec.get("cache")
        return str(cache.get("status") or "?") if isinstance(cache, dict) else "?"
    if by == "session":
        return str(rec.get("session") or "?")
    if by == "day":
        return str(rec.get("ts") or "?")[:10]
    return str(rec.get("model") or "?")


def _count_cache(bucket: dict, rec: dict) -> None:
    """Fold one record's cache outcome into a bucket. Shared so a breaker-shed
    call — excluded from calls, errors and latency — still has its outer cache
    lookup counted; dropping those under-counted misses and inflated the hit
    rate over a fraction of the real lookups."""
    cache = rec.get("cache")
    if not isinstance(cache, dict):
        return
    if cache.get("status") == "hit":
        bucket["cache_hits"] += 1
    elif cache.get("status") == "miss":
        bucket["cache_misses"] += 1


def _p95(sorted_ms: list[int]) -> int | None:
    """Nearest-rank p95 over an ascending list, or None when empty."""
    if not sorted_ms:
        return None
    idx = max(0, int(len(sorted_ms) * 0.95 + 0.5) - 1)
    return sorted_ms[min(idx, len(sorted_ms) - 1)]


def _finalize(key: str, b: dict) -> dict:
    lat = sorted(b["lat"])
    calls = b["calls"]
    cache_lookups = b["cache_hits"] + b["cache_misses"]
    return {
        "key": key,
        "calls": calls,
        "allowed": b["allowed"],
        "refused": b["refused"],
        "errors": b["errors"],
        "circuit_open": b["circuit_open"],
        "cancelled": b["cancelled"],
        "avg_ms": int(sum(lat) / len(lat)) if lat else None,
        "p95_ms": _p95(lat),
        "error_rate": round(b["errors"] / calls, 3) if calls else 0.0,
        # Shed calls are not errors, so error_rate cannot carry this: without a
        # rate of its own an oracle shed for the whole window shows calls=0 and
        # error_rate=0.0 — every health column green on the one dead backend.
        "shed_rate": (
            round(b["circuit_open"] / (calls + b["circuit_open"]), 3)
            if (calls + b["circuit_open"])
            else 0.0
        ),
        "cache_hits": b["cache_hits"],
        "cache_misses": b["cache_misses"],
        "cache_hit_rate": round(b["cache_hits"] / cache_lookups, 3) if cache_lookups else 0.0,
        "total_tokens": b["total_tokens"],
        "cost_usd": round(b["cost_usd"], 6),
        "usage_coverage": b["usage_coverage"],
        "consensus_counts": b["consensus_counts"],
        "synth_fallback_true": b["synth_fallback_true"],
    }


def _empty_bucket() -> dict:
    return {
        "calls": 0,
        "allowed": 0,
        "refused": 0,
        "errors": 0,
        "circuit_open": 0,
        "cancelled": 0,
        "lat": [],
        "cache_hits": 0,
        "cache_misses": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "usage_coverage": 0,
        "consensus_counts": {},
        "synth_fallback_true": 0,
    }


def aggregate(
    path: Path,
    *,
    window: str = "24h",
    by: str = "model",
    model_filter: str | None = None,
    session_filter: str | None = None,
) -> dict:
    """Stream the audit log and aggregate it into buckets (constant memory per
    record — the log is never materialized as a whole).

    ``window`` bounds how far back to look (``all`` = no bound); ``by`` picks the
    bucket key — ``model`` | ``provider`` | ``tool`` | ``session`` | ``day`` |
    ``project`` | ``cache`` | ``mode``, where ``provider`` is the per-backend-call
    view that sees council, chain and debate members one by one; the filters
    narrow to one
    model/session before bucketing. Guard denials are counted under ``refused``
    (a denial IS a refusal, just at the guard stage). Latency stats cover only
    records that carry ``duration_ms`` (guard denials don't). Calls the circuit
    breaker shed (``error.kind == "circuit_open"``) are counted under
    ``circuit_open`` only — never as calls, errors, or latency; they reach
    ``totals`` even under a ``by`` that cannot bucket them, and carry their own
    ``shed_rate`` because ``error_rate`` cannot represent them. A call a caller's
    cap cancelled counts under ``cancelled`` rather than ``errors``: the latency
    is real but the failure was local, which is why the circuit breaker ignores
    those too."""
    window = window if window in WINDOWS else "24h"
    by = by if by in BY else "model"
    cutoff = None
    if window != "all":
        cutoff = datetime.now(UTC).timestamp() - _WINDOW_S[window]

    buckets: dict[str, dict] = {}
    totals = _empty_bucket()

    # Two streaming passes instead of materializing every record: the log plus its
    # rotated segments can be arbitrarily large (segments are retained indefinitely
    # by default), and a full list of parsed dicts can OOM a modest machine. Pass 1
    # only finds the v2-era start; pass 2 buckets one record at a time.
    v2_start: float | None = None
    for rec in _iter_records(path):
        if rec.get("schema_version") == 2 and rec.get("event_name") == "tool.completed":
            ts = _parse_ts(str(rec.get("timestamp") or ""))
            if ts is not None and (v2_start is None or ts < v2_start):
                v2_start = ts

    for rec in _iter_records(path):
        if by == "provider" and rec.get("schema_version") != 2:
            continue
        if rec.get("schema_version") == 2:
            expected_event = "provider.completed" if by == "provider" else "tool.completed"
            if rec.get("event_name") != expected_event:
                continue
            if by == "provider" and isinstance(rec.get("provider"), dict):
                if rec["provider"].get("transport") == "cache":
                    continue
            rec = dict(rec)
            rec["ts"] = rec.get("timestamp")
            rec["decision"] = "allowed" if rec.get("status") == "ok" else rec.get("status")
            # v2 events carry a top-level "mode" = trace CAPTURE mode (safe/full);
            # by="mode" means orchestration mode (council/chain/debate) — reset it
            # so single-model calls bucket under "?" instead of safe/full.
            rec["mode"] = None
            orchestration = rec.get("orchestration")
            if isinstance(orchestration, dict):
                if isinstance(orchestration.get("model"), str):
                    rec["model"] = orchestration["model"]
                if isinstance(orchestration.get("mode"), str):
                    rec["mode"] = orchestration["mode"]
                if isinstance(orchestration.get("quorum"), str):
                    rec["quorum"] = orchestration["quorum"]
                if isinstance(orchestration.get("consensus"), str):
                    rec["consensus"] = orchestration["consensus"]
                if isinstance(orchestration.get("synth_fallback"), bool):
                    rec["synth_fallback"] = orchestration["synth_fallback"]
        elif v2_start is not None:
            legacy_time = _parse_ts(str(rec.get("ts") or ""))
            if legacy_time is not None and legacy_time >= v2_start:
                continue
        if cutoff is not None:
            ts = _parse_ts(str(rec.get("ts") or ""))
            if ts is None or ts < cutoff:
                continue
        if model_filter and str(rec.get("model") or "") != model_filter:
            continue
        if session_filter and str(rec.get("session") or "") != session_filter:
            continue

        key = _bucket_key(rec, by)
        # A record this view can't bucket (an errored call carries no model; a
        # non-cache record has no cache status) is dropped from the breakdown.
        unbucketed = (by == "cache" and key not in {"hit", "miss"}) or (
            by == "model" and key == "?"
        )
        error = rec.get("error")
        if isinstance(error, dict) and error.get("kind") == "circuit_open":
            # The breaker shed this call — no backend was touched. Counting it as
            # an error (and its ~0 ms as latency) made a tripped oracle look both
            # worse and faster than it was, for as long as the breaker stayed
            # open. It goes under ``circuit_open`` and nowhere else — but it still
            # reaches TOTALS even in a view that can't bucket it, or the default
            # by="model" would report a shed-only window as no traffic at all.
            targets = (
                (totals,) if unbucketed else (buckets.setdefault(key, _empty_bucket()), totals)
            )
            for tgt in targets:
                tgt["circuit_open"] += 1
                _count_cache(tgt, rec)
            continue
        if unbucketed:
            continue
        b = buckets.setdefault(key, _empty_bucket())
        cancelled = isinstance(error, dict) and error.get("kind") == "cancelled"
        decision = str(rec.get("decision") or "")
        dur = rec.get("duration_ms")
        for tgt in (b, totals):
            tgt["calls"] += 1
            if decision == "allowed":
                tgt["allowed"] += 1
            elif decision in ("refused", "denied"):
                tgt["refused"] += 1
            elif cancelled:
                # A caller's cap stopped this, so it is a call that happened (the
                # latency is real) but NOT evidence the backend is failing — the
                # same reason cancellations are kept away from the breaker.
                tgt["cancelled"] += 1
            elif decision == "error":
                tgt["errors"] += 1
            if isinstance(dur, (int, float)):
                tgt["lat"].append(int(dur))
            _count_cache(tgt, rec)
            usage = rec.get("usage")
            if isinstance(usage, dict):
                tgt["usage_coverage"] += 1
                total_tokens = usage.get("total_tokens")
                cost = usage.get("cost_usd")
                if isinstance(total_tokens, (int, float)):
                    tgt["total_tokens"] += int(total_tokens)
                if isinstance(cost, (int, float)):
                    tgt["cost_usd"] += float(cost)
            consensus = rec.get("consensus")
            if isinstance(consensus, str) and consensus:
                tgt["consensus_counts"][consensus] = tgt["consensus_counts"].get(consensus, 0) + 1
            if rec.get("synth_fallback") is True:
                tgt["synth_fallback_true"] += 1

    out_buckets = [_finalize(k, b) for k, b in buckets.items()]
    # Days read chronologically; everything else by traffic. Shed calls count
    # toward that traffic even though they are not calls: an oracle whose breaker
    # was open for the whole window has calls=0, and sorting on calls alone put
    # the one dead backend at the BOTTOM of the list, below every healthy one.
    if by == "day":
        out_buckets.sort(key=lambda x: x["key"])
    else:
        out_buckets.sort(key=lambda x: (-(x["calls"] + x["circuit_open"]), x["key"]))
    return {
        "status": "ok",
        "window": window,
        "by": by,
        "buckets": out_buckets,
        "totals": _finalize("all", totals),
    }
