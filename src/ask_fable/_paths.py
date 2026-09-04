"""Shared filesystem primitives — XDG fallback, atomic writes, secure modes.

Centralized so the cache / context store / outputs / sessions / config / audit
modules all agree on:
  - where files live (``$XDG_STATE_HOME/ask_fable/`` etc.)
  - how new files are opened (mode ``0o600`` atomically, never world-readable
    for a window)
  - how existing files are rewritten (``tempfile + os.replace`` + ``fsync``)
  - how a parent directory is created with mode ``0o700``

Best-effort everywhere: every primitive returns ``None`` / a falsy on failure
rather than raising, so the caller decides whether the failure is fatal.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Final


def xdg_state_dir() -> Path:
    """``$XDG_STATE_HOME`` or ``~/.local/state`` — the per-user state root."""
    return Path(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"))


def xdg_config_dir() -> Path:
    """``$XDG_CONFIG_HOME`` or ``~/.config`` — the per-user config root."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))


def ensure_dir_secure(d: Path) -> bool:
    """Create ``d`` (parents included) with mode ``0o700``. Returns True on
    success; on failure (permissions / unsupported FS) returns False and never
    raises. The directory's mode is enforced via ``os.chmod`` after ``mkdir`` —
    on filesystems that don't support POSIX perms the chmod fails silently and
    the directory is left at the process umask."""
    try:
        # mode= closes the create-at-umask-then-chmod window (mkdir applies it
        # atomically); the chmod still tightens a pre-existing directory.
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return True
    except OSError:
        print("ask_fable: storage operation failed: category=directory_create", file=sys.stderr)
        return False


def _chmod_best_effort(p: Path) -> None:
    """Tighten perms to ``0o600`` if the FS supports it. Silent on failure."""
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def secure_sqlite_sidecars(db_path: Path) -> None:
    """Chmod a SQLite database and its ``-wal``/``-shm``/``-journal`` sidecars
    to ``0o600`` — best-effort, silent on failure.

    SQLite creates the WAL/SHM sidecars itself at the process umask (typically
    ``0o644``), and they hold the most recent writes (full hub Q&A text,
    context blobs). The ``0o700`` parent dir is the primary access boundary —
    this is defense-in-depth so the documented ``0o600`` file-mode claim also
    covers the sidecars. Called after each store's ``_connect()`` (and the WAL
    pragma that creates them); a clean close can delete and recreate the WAL at
    umask, so ``serve()`` also sets a restrictive process umask."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            _chmod_best_effort(p)


def _fsync_dir(d: Path) -> None:
    """``fsync`` a directory — best-effort. POSIX requires this for the rename
    of a child to be durable across a crash. Silently no-op on Windows or FS
    that don't support it."""
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def write_secure(path: Path, content: str | bytes, *, mode: int = 0o600) -> bool:
    """Atomically write ``content`` to ``path`` with ``mode`` (default 0o600).

    Implementation: write to ``path`` in the same directory (so ``os.replace``
    is atomic on the same FS), ``fsync`` the file, then ``os.replace`` onto
    the target, then ``fsync`` the parent dir so the rename is durable.

    Why this matters: a crash mid-``write_text`` can leave a half-written file
    that ``read_text`` then returns as parseable-but-truncated, which the old
    ``outputs.py``/``sessions.py``/``config.py`` code would silently turn into
    a corrupt / empty answer / session dump / ``{}`` config (silent total loss
    of the user's council choice — the worst of the four).

    Returns True on success, False on any failure (never raises). The file is
    only chmod'd in the temp-file stage (no TOCTOU window where it exists at
    the process umask before the chmod).
    """
    data = content.encode("utf-8") if isinstance(content, str) else content
    parent = path.parent
    if not ensure_dir_secure(parent):
        return False
    try:
        # NamedTemporaryFile(delete=False) creates the file mode 0o600 by default on
        # POSIX, so the window where the file is world-readable never opens.
        fd, tmp_str = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
        tmp = Path(tmp_str)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
            os.replace(tmp, path)
            _fsync_dir(parent)
            return True
        except Exception:
            # Clean up the orphan temp file on any mid-write failure.
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
    except OSError:
        print("ask_fable: storage operation failed: category=atomic_write", file=sys.stderr)
        return False


def open_append_secure(path: Path) -> object | None:
    """Open ``path`` for appending, creating it with mode ``0o600`` if missing.

    Solves the TOCTOU in the old ``audit.py``: opening with ``open("a")`` first
    creates the file at the process umask (often ``0o644``) and *then* we
    chmod — leaving a world-readable window. This primitive:
      1. Creates the parent dir securely (mode 0o700).
      2. If the file is missing, creates it first at mode 0o600 (no window).
      3. Then opens it in append mode (which is atomic up to PIPE_BUF).

    Returns a file handle (the caller is responsible for closing) or ``None``
    on failure. POSIX-only best-effort; on Windows the mode is silently lost.
    """
    parent = path.parent
    if not ensure_dir_secure(parent):
        return None
    try:
        if not path.exists():
            # Pre-create with mode 0o600 so there's no world-readable window.
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        return open(path, "a", encoding="utf-8")
    except OSError:
        print("ask_fable: storage operation failed: category=append_open", file=sys.stderr)
        return None


# Sentinel — exported for tests and for callers that want to assert on the
# canonical "secure" mode.
SECURE_FILE_MODE: Final = 0o600
SECURE_DIR_MODE: Final = 0o700


def now_iso_z() -> str:
    """ISO-8601 timestamp with a ``Z`` suffix (UTC). Shared by audit, outputs,
    and sessions so they don't each roll their own ``.replace('+00:00', 'Z')``."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def truthy(name: str, default: bool = False) -> bool:
    """Read a boolean env var. ``1|true|yes|on`` (case-insensitive) → True;
    everything else → ``default``."""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def int_env(name: str, default: int = 0) -> int:
    """Read a non-negative integer env var. Anything unparseable → ``default``;
    negative values clamp to 0 (disabled). Used for optional retention caps."""
    try:
        n = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return max(0, n)


def prune_dir(d: Path, *, keep: int, pattern: re.Pattern[str]) -> int:
    """Delete the oldest files whose names fully match ``pattern`` in ``d``
    beyond the ``keep`` newest. Returns the number deleted (0 when the cap is
    disabled). Only names matching the caller's own filename shape are
    candidates — a shared or user-pointed directory may hold files this tool
    did not write, and retention must never delete those. Best-effort per
    file: an OSError while statting/deleting one file (e.g. a concurrent
    pruner already unlinked it) skips that file only — retention must never
    break the tool."""
    if keep <= 0:
        return 0
    files: list[tuple[float, Path]] = []
    try:
        entries = os.scandir(d)
    except OSError:
        return 0
    with entries:
        for entry in entries:
            if not pattern.fullmatch(entry.name):
                continue
            try:
                if entry.is_file():
                    files.append((entry.stat().st_mtime, Path(entry.path)))
            except OSError:
                continue
    if len(files) <= keep:
        return 0
    files.sort(key=lambda x: x[0])
    excess = len(files) - keep
    deleted = 0
    for _, p in files[:excess]:
        try:
            p.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted
