"""The controlled spawn environment every oracle bridge shares.

A CLI oracle must not inherit the calling agent's cwd. Claude Code walks *up* from cwd
to discover CLAUDE.md/AGENTS.md, Codex reads AGENTS.md from the workspace root, and agy
resolves a project — all keyed to cwd. Spawning in one empty, ask_fable-owned directory
(whose ancestors carry no instruction file) means a model can only ever see what the
caller passed in the prompt, never the surrounding repo.

Claude Code-specific opt-outs (``--safe-mode`` and the auto-memory/skill env vars) live
in :mod:`ask_fable.fable`; this module owns the part that is transport-agnostic.
"""

from __future__ import annotations

from pathlib import Path

from . import _paths


def oracle_cwd() -> Path:
    """The controlled cwd every oracle subprocess spawns in.

    A stable, empty dir under ask_fable's own state dir. Created on demand; if it can't
    be created (a read-only state dir), the parent is returned as a best-effort fallback
    rather than failing the turn — callers that need a hard guarantee also pass CLI-level
    flags (``--safe-mode``, ``--ignore-user-config``)."""
    d = _paths.xdg_state_dir() / "ask_fable" / "oracle-cwd"
    # ensure_dir_secure, not a bare mkdir: outside the server's 0o077 umask (a script
    # importing this) a plain mkdir left the ask_fable state dir world-readable, and
    # nothing re-tightens an existing directory any more.
    if not _paths.ensure_dir_secure(d):
        return d.parent
    return d
