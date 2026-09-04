"""Persist every answer to disk — one markdown file per successful reply.

Unlike ``sessions.py`` (which only dumps a transcript when a session is reset)
this keeps a durable copy of each ``ask`` / ``ask_m3`` / ``ask_ollama`` /
council answer, so an output is never lost to the ephemeral chat transcript.
The reasoning trace (``thinking``) is captured too — for a council, both the
synthesizer's trace and each panelist's — since it's otherwise discarded.

Location: ``$ASK_FABLE_OUTPUT_DIR`` if set, else
``${XDG_STATE_HOME:-~/.local/state}/ask_fable/answers/``. Files are written
0600 (dir 0700) via an atomic rename (``tempfile + os.replace + fsync``) so
a crash mid-write can never leave a partial/corrupt markdown file. Saving is
best-effort: a write error is warned to stderr and swallowed — persistence
must never break the tool. Set ``ASK_FABLE_SAVE=0`` to turn it off.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import _paths
from .telemetry import redact_text
from .trace_runtime import current

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _output_dir() -> Path:
    override = os.environ.get("ASK_FABLE_OUTPUT_DIR")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_state_dir() / "ask_fable" / "answers"


def _enabled() -> bool:
    explicit = os.environ.get("ASK_FABLE_SAVE")
    if explicit is not None:
        return explicit.strip().lower() not in ("0", "false", "no", "off")
    return (os.environ.get("ASK_FABLE_TRACE_MODE") or "safe").strip().lower() == "full"


def _max_files() -> int:
    """Max answer markdown files to keep in the output dir (0 = unlimited)."""
    return _paths.int_env("ASK_FABLE_MAX_ANSWERS", 0)


# Matches only names save() itself writes: {tool}-{model}-{trace_id|stamp}.md,
# where the tail is a uuid4 trace id or a %Y%m%dT%H%M%S%fZ stamp. The retention
# prune uses this so files ask-fable did not write (a user-pointed
# ASK_FABLE_OUTPUT_DIR may hold their own notes) are never deleted.
_OWNED_RE = re.compile(
    r"[A-Za-z0-9._-]+-[A-Za-z0-9._-]+-"
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{8}T\d{12}Z)\.md"
)


def _slug(text: str, fallback: str) -> str:
    return _SLUG_RE.sub("_", text).strip("_") or fallback


def save(
    *,
    tool: str,
    model: str,
    question: str,
    answer: str,
    context: str = "",
    session: str = "",
    thinking: str = "",
    sources: list[dict] | None = None,
) -> str | None:
    """Write one answer to a timestamped markdown file. Returns its path, or
    None when disabled or on any failure (best-effort, never raises)."""
    if not _enabled():
        return None
    try:
        d = _output_dir()
        if not _paths.ensure_dir_secure(d):
            return None
        now = datetime.now(UTC)
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        trace = current()
        suffix = trace.trace_id if trace is not None else stamp
        name = f"{_slug(tool, 'ask')}-{_slug(model, 'model')}-{suffix}.md"
        path = d / name
        question = redact_text(question)[0]
        answer = redact_text(answer)[0]
        context = redact_text(context)[0]
        thinking = redact_text(thinking)[0]
        lines = [
            f"# ask_fable answer — {tool}",
            "",
            f"- **timestamp:** {_paths.now_iso_z()}",
            f"- **tool:** {tool}",
            f"- **model:** {model}",
        ]
        if trace is not None:
            lines.append(f"- **trace_id:** {trace.trace_id}")
        if session:
            lines.append(f"- **session:** {session}")
        lines += ["", "## Question", "", question or "(empty)"]
        if context:
            lines += ["", "## Context", "", context]
        lines += ["", "## Answer", "", answer or "(empty)"]
        if thinking:
            lines += ["", "## Thinking", "", thinking]
        if sources:
            lines += ["", "## Sources"]
            for s in sources:
                m = s.get("model", "?")
                if s.get("status") == "ok":
                    lines += ["", f"### {m}", "", redact_text(str(s.get("answer", "")))[0]]
                else:
                    detail = s.get("reason") or s.get("detail") or s.get("kind") or ""
                    lines += [
                        "",
                        f"### {m} — {s.get('status', '?')}",
                        "",
                        redact_text(str(detail))[0],
                    ]
                if s.get("thinking"):
                    lines += ["", f"#### {m} — thinking", "", redact_text(str(s["thinking"]))[0]]
        # Atomic write — no partial file on crash. _paths.write_secure
        # creates the temp file at mode 0o600 and renames onto the target.
        if _paths.write_secure(path, "\n".join(lines)):
            if trace is not None:
                trace.stage("persistence.markdown", "ok")
            # Retention cap — off by default; prune only after the new file
            # is durable so a failed prune can't drop the just-written answer.
            keep = _max_files()
            if keep:
                pruned = _paths.prune_dir(d, keep=keep, pattern=_OWNED_RE)
                if pruned and trace is not None:
                    trace.stage(
                        "persistence.markdown.prune", "ok", orchestration={"deleted": pruned}
                    )
            return str(path)
        if trace is not None:
            trace.persistence_failure("markdown_write")
        return None
    except Exception as exc:  # noqa: BLE001 — saving must never break the tool
        trace = current()
        if trace is not None:
            trace.persistence_failure("markdown_write")
        print(f"ask_fable: answer save failed: {exc}", file=sys.stderr)
        return None
