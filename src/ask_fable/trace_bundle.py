from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from . import _paths, config
from .telemetry import JSONValue, redact_text

_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|authorization|cookie|password|secret|token)")


def directory() -> Path:
    override = os.environ.get("ASK_FABLE_TRACE_DIR")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_state_dir() / "ask_fable" / "traces"


def redact_value(value: JSONValue, key: str = "") -> JSONValue:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    match value:
        case str():
            return redact_text(value)[0]
        case list():
            return [redact_value(item) for item in value]
        case dict():
            return {name: redact_value(item, name) for name, item in value.items()}
        case _:
            return value


def _max_bytes() -> int:
    try:
        return max(1, int(os.environ.get("ASK_FABLE_TRACE_MAX_CONTENT_BYTES") or 104_857_600))
    except ValueError:
        return 104_857_600


# Bundle files are "{trace_id}.json"; trace ids are the validated [A-Za-z0-9-]{1,64}.
# Anchored so retention only ever deletes files this module itself wrote.
_BUNDLE_RE = re.compile(r"[A-Za-z0-9-]{1,64}\.json")


def _max_bundles() -> int:
    """Retention cap for full-mode trace bundles. Default 0 = unlimited (preserves
    prior behaviour); set ASK_FABLE_TRACE_MAX_BUNDLES>0 to keep only the newest N and
    stop unbounded disk growth (every full-mode tool call writes a never-deleted file)."""
    try:
        return int(os.environ.get("ASK_FABLE_TRACE_MAX_BUNDLES") or 0)
    except (TypeError, ValueError):
        return 0


def write(trace_id: str, content: dict[str, JSONValue]) -> Path | None:
    # config-over-env, matching ToolTrace.mode and the reported "effective" mode — so
    # configure_tracing(trace_mode=...) actually governs whether bundles hit disk.
    # Reading os.environ directly here was the split-brain: a runtime "safe" toggle
    # still wrote bundles, and "full" set via config with env unset wrote none.
    if (config.setting("ASK_FABLE_TRACE_MODE") or "safe").strip().lower() != "full":
        return None
    try:
        redacted = redact_value(content)
        encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
        cap = _max_bytes()
        truncated = len(encoded.encode("utf-8")) > cap
        payload: JSONValue = (
            {"content": encoded.encode("utf-8")[:cap].decode("utf-8", errors="ignore"), "truncated": True}
            if truncated
            else {"content": redacted, "truncated": False}
        )
        target_dir = directory()
        if target_dir.is_symlink():
            return None
        if not _paths.ensure_dir_secure(target_dir):
            return None
        path = target_dir / f"{trace_id}.json"
        if path.is_symlink() or path.parent.resolve() != target_dir.resolve():
            return None
        if not _paths.write_secure(path, json.dumps(payload, ensure_ascii=False)):
            return None
        # F4: bundles were never pruned — one per full-mode call, forever. Best-effort
        # retention (no-op unless ASK_FABLE_TRACE_MAX_BUNDLES is set).
        _paths.prune_dir(target_dir, keep=_max_bundles(), pattern=_BUNDLE_RE)
        return path
    except (OSError, TypeError, ValueError):
        return None


def read(trace_id: str, max_chars: int) -> tuple[str, bool] | None:
    target_dir = directory()
    path = target_dir / f"{trace_id}.json"
    if target_dir.is_symlink() or path.is_symlink():
        return None
    try:
        if path.parent.resolve() != target_dir.resolve():
            return None
    except OSError:
        return None
    limit = max(1, min(max_chars, 50_000))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return None
            raw = source.read(limit * 4 + 1)
        text = raw.decode("utf-8", errors="ignore")
    except OSError:
        return None
    return text[:limit], len(text) > limit or len(raw) > limit * 4
