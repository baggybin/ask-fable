"""A tiny, tool-writable JSON config store.

Everything in ask_fable is configurable by environment variable, but env vars
are set once at MCP-registration time and can't be changed from inside a running
session. This store adds a small persisted layer an agent CAN write at runtime —
so the ``configure_ollama_council`` tool can save the user's chosen council and
have it stick across sessions without anyone hand-editing ``~/.claude.json``.

Precedence, highest first: this config file → environment variable → built-in
default (see ``ollama.council_models`` / ``ollama.default_model``). The file is
optional; a missing or unparseable file reads as "no overrides" and never raises.

Location: ``$ASK_FABLE_CONFIG_FILE`` if set, else
``${XDG_CONFIG_HOME:-~/.config}/ask_fable/config.json``. Written 0600 (dir 0700)
via an atomic rename (``tempfile + os.replace + fsync``) — a crash mid-write
can never leave a half-written JSON that the next ``load()`` would silently
turn into ``{}`` (the old behavior, which could lose the user's entire council
choice without warning).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import _paths


def path() -> Path:
    """The config file path (may not exist yet)."""
    override = os.environ.get("ASK_FABLE_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    return _paths.xdg_config_dir() / "ask_fable" / "config.json"


def load() -> dict:
    """Return the config dict, or ``{}`` when absent/unreadable (never raises)."""
    p = path()
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def save(patch: dict) -> str | None:
    """Merge ``patch`` into the config file and write it back. Returns the path
    on success, or None on failure (best-effort — a config write must never break
    a tool call). Keys whose value is None are removed.

    The atomic write (tempfile + os.replace + fsync) is the durability fix: a
    crash mid-save can never leave a half-written file that load() would
    silently turn into ``{}``. Two concurrent writers both race to read the
    current state, merge their own patch, and atomically rename; last-writer
    wins for the field each touched, and both files stay parseable."""
    p = path()
    try:
        current = load()
        for k, v in patch.items():
            if v is None:
                current.pop(k, None)
            else:
                current[k] = v
        if _paths.write_secure(p, json.dumps(current, indent=2) + "\n"):
            return str(p)
        return None
    except Exception as exc:  # noqa: BLE001 — persistence must never break the tool
        print(f"ask_fable: config save failed: {exc}", file=sys.stderr)
        return None


def get_str(key: str) -> str | None:
    """A non-empty string config value, or None."""
    v = load().get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def setting(key: str) -> str | None:
    """Effective value for ``key`` following the documented precedence: runtime
    config file first, then environment variable, else None. The single place that
    resolves config-over-env, so any string/flag setting can be toggled at runtime
    (e.g. by ``configure_tracing``) without editing the MCP registration."""
    return get_str(key) or (os.environ.get(key) or "").strip() or None


def get_list(key: str) -> list[str] | None:
    """A non-empty list-of-strings config value, or None. Accepts either a JSON
    array or a comma/space-separated string for convenience."""
    v = load().get(key)
    if isinstance(v, list):
        out = [str(x).strip() for x in v if str(x).strip()]
        return out or None
    if isinstance(v, str) and v.strip():
        out = [p.strip() for p in v.replace(",", " ").split() if p.strip()]
        return out or None
    return None
