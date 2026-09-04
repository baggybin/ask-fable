"""Append-only decision log for ask_fable.

One JSONL record per call — allowed / denied / refused / error — so an operator
can review what was asked of Fable and what the gate did. The question is HASHED
by default (code may be proprietary); set ``ASK_FABLE_AUDIT_RAW=1`` to store the
raw text. ``ASK_FABLE_AUDIT_RAW_CONTEXT`` splits the switch: context (the larger
proprietary-code / secret-bearing surface) can stay hashed-only while questions are
stored raw. Council/chain records also carry ``quorum``/``consensus``/
``synth_fallback`` so the ``stats`` tool can aggregate degradation. Audit I/O never
fails the tool: a write error is warned to stderr and swallowed.

Durability: the directory is restricted to ``0o700`` and both the active file and
dedicated lock file are repaired to ``0o600`` on use. One exclusive lock covers
size checking, immutable timestamp/sequence rotation, append, and file/directory
``fsync``. Rotated segments are retained indefinitely by default. Explicitly set
``ASK_FABLE_AUDIT_BACKUPS`` to a finite count, including zero, to cap retention.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path

from . import _paths
from .telemetry import CaptureMode, ContentCapture, TraceEvent, capture_content, redact_text
from .trace_store import EventStore, StoredRecord

__all__ = [
    "CaptureMode",
    "ContentCapture",
    "EventStore",
    "StoredRecord",
    "TraceEvent",
    "audit_path",
    "capture_content",
    "record",
]


def _state_dir() -> Path:
    return _paths.xdg_state_dir() / "ask_fable"


def audit_path() -> Path:
    override = os.environ.get("ASK_FABLE_AUDIT_PATH")
    if override:
        return Path(override)
    return _state_dir() / "decisions.jsonl"


def _raw_enabled() -> bool:
    return (os.environ.get("ASK_FABLE_AUDIT_RAW") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _raw_context_enabled() -> bool:
    """Whether ``context_raw`` is stored. Defaults to ``ASK_FABLE_AUDIT_RAW`` so the
    single switch keeps working; ``ASK_FABLE_AUDIT_RAW_CONTEXT`` overrides it either
    way. Split out because context is the larger proprietary-code surface (and the
    likelier one to carry secrets) — an operator can keep raw questions for
    debugging while context stays hashed-only."""
    raw = (os.environ.get("ASK_FABLE_AUDIT_RAW_CONTEXT") or "").strip().lower()
    if not raw:
        return _raw_enabled()
    return raw in ("1", "true", "yes", "on")


def _max_bytes() -> int:
    try:
        return int(os.environ.get("ASK_FABLE_AUDIT_MAX_BYTES") or 52428800)
    except (TypeError, ValueError):
        return 52428800


def _backups() -> int | None:
    raw = os.environ.get("ASK_FABLE_AUDIT_BACKUPS")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def event_store() -> EventStore:
    return EventStore(path=audit_path(), max_bytes=_max_bytes(), retain_segments=_backups())


def record(
    *,
    decision: str,
    stage: str | None,
    reason: str,
    question: str,
    context: str = "",
    session: str = "default",
    # Unspecified rather than a hardcoded id: every real caller passes the
    # model that actually ran, and a pinned default silently misattributes
    # the row once that id moves.
    model: str = "",
    duration_ms: int | None = None,
    outcome_detail: str = "",
    quorum: str | None = None,
    consensus: str | None = None,
    synth_fallback: bool | None = None,
    trusted_session: bool = False,
) -> None:
    """Append one decision record. Best-effort — never raises.

    ``quorum`` (e.g. ``"2/3"``), ``consensus`` (``strong``/``partial``/``divergent``/
    ``unknown``), and ``synth_fallback`` are council/chain-only enrichments — written
    to the record only when provided, so single-tool records stay slim. They let the
    ``stats`` tool answer "what fraction of my councils reached strong consensus?"
    and "how often does synthesis fall back?"."""
    try:
        from .trace_runtime import current

        if current() is not None:
            return
        path = audit_path()
        if not _paths.ensure_dir_secure(path.parent):
            return
        rec: dict[str, object] = {
            "ts": _paths.now_iso_z(),
            "event_id": str(uuid.uuid4()),
            "decision": decision,
            "stage": stage,
            "reason": redact_text(reason)[0],
            "session": session,
            "model": model,
            "question_len": len(question or ""),
            "context_len": len(context or ""),
            "question_sha256": hashlib.sha256((question or "").encode("utf-8")).hexdigest(),
            "duration_ms": duration_ms,
            "outcome_detail": redact_text(outcome_detail)[0],
        }
        if quorum is not None:
            rec["quorum"] = quorum
        if consensus is not None:
            rec["consensus"] = consensus
        if synth_fallback is not None:
            rec["synth_fallback"] = synth_fallback
        if trusted_session:
            rec["trusted_session"] = True
        if _raw_enabled():
            rec["question_raw"] = redact_text(question)[0]
        if _raw_context_enabled():
            rec["context_raw"] = redact_text(context)[0]
        event_store().append(rec)
    except Exception as exc:  # noqa: BLE001 — audit must never break the tool
        print(f"ask_fable: audit write failed: {exc}", file=sys.stderr)
