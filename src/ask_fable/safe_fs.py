"""The filesystem safety kernel for server-side context packing.

This is ask_fable's FIRST filesystem-aware surface. The reasoning backends stay
blind to the repo; only the *server*, running locally next to it, reads files —
and only on the calling agent's explicit request, only within one configured
project root. Everything an oracle ever sees still arrives as text, so the trust
boundary does not move; this module's whole job is to make "read a repo file"
provably safe.

The containment check is the standard shape: realpath the target AND the root,
then require ``os.path.commonpath([root, target]) == root``. Realpathing the root
too is load-bearing — a symlinked root (macOS ``/tmp -> /private/tmp``, a symlinked
checkout) otherwise fails containment for *every* legitimate file. The blocklist
runs on the post-realpath, root-relative path (component-anchored, never substring)
so a symlink ``notes.txt -> .env`` can't smuggle a secret file past it.

Every rejection is a typed ``Reason`` (never an exception out of a call): callers
put it straight into a machine-parseable ``skipped[]`` without string-matching.
The same enum is shared with the budget layer (``resolver.py``) so ``skipped``
reasons stay consistent across both.

Residual TOCTOU: ``resolve_within`` validates by path and ``read_content`` opens
by path, so a symlink swap between the two is theoretically possible. For a local,
single-process tool where the agent already has filesystem access this is an
accepted, documented risk, not a hole — containment still fails closed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Per-file whole-file read cap (bytes). A whole-file spec larger than this is
# rejected TOO_LARGE before its bytes are slurped; a range spec is capped on the
# bytes it actually collects, so a small slice of a huge file still succeeds.
DEFAULT_MAX_FILE_BYTES = 1_000_000


class Reason(StrEnum):
    """Why a spec was rejected. ``StrEnum`` so it serializes straight to JSON.

    ``escape``..``bad_range`` are emitted here; ``budget_exhausted`` and
    ``max_files`` are emitted by the budget layer (``resolver.py``) — kept in one
    enum so ``skipped[]`` reasons are consistent across both."""

    ESCAPE = "escape"            # resolved outside the project root
    NOT_FILE = "not_file"        # missing, a directory, or an empty path
    UNREADABLE = "unreadable"    # OS error opening/reading
    TOO_LARGE = "too_large"      # exceeds the per-file byte cap
    BINARY = "binary"            # contains a NUL byte
    BLOCKED = "blocked"          # matched the blocklist (.git/, .env*)
    BAD_RANGE = "bad_range"      # malformed or out-of-file line range
    BUDGET_EXHAUSTED = "budget_exhausted"  # (resolver) bundle char budget hit
    MAX_FILES = "max_files"      # (resolver) too many files in one pack


@dataclass(frozen=True)
class Resolved:
    """A validated, in-root file target. ``start``/``end`` are 1-indexed inclusive
    line bounds, or both ``None`` for the whole file."""

    abs_path: str
    rel_path: str
    start: int | None = None
    end: int | None = None


# A full, well-formed range suffix: `path:START-END`, right-anchored so a Windows
# drive (`C:\...`) or a colon-bearing filename can't misparse as a range.
_RANGE_RE = re.compile(r"^(.+):(\d+)-(\d+)$")
# A suffix that LOOKS like an attempted range (digits and/or a dash) but isn't a
# well-formed one — `foo.py:10`, `foo.py:10-`, `foo.py:-5`. Rejected loudly rather
# than silently packing the whole file.
_ATTEMPT_RE = re.compile(r"^\d*-?\d*$")


def max_file_bytes() -> int:
    try:
        v = int(os.environ.get("ASK_FABLE_PACK_MAX_FILE_BYTES") or DEFAULT_MAX_FILE_BYTES)
        return v if v > 0 else DEFAULT_MAX_FILE_BYTES
    except (TypeError, ValueError):
        return DEFAULT_MAX_FILE_BYTES


def resolve_root() -> str | None:
    """The configured project root, realpath'd and confirmed to be a directory, or
    None when unconfigured/invalid. Precedence mirrors the rest of the codebase:
    config file -> env var. Read per call (never cached) so a runtime config edit
    takes effect without a restart."""
    from . import config  # local import keeps the safety kernel dependency-light

    raw = config.get_str("project_root") or os.environ.get("ASK_FABLE_PROJECT_ROOT")
    if not raw or not raw.strip():
        return None
    try:
        rp = os.path.realpath(os.path.expanduser(raw.strip()))
    except OSError:
        return None
    return rp if os.path.isdir(rp) else None


def _parse_spec(spec: str) -> tuple[str, int | None, int | None] | Reason:
    """Split a spec into (path, start, end). ``start``/``end`` are None for a whole
    file. A malformed range suffix returns ``Reason.BAD_RANGE`` rather than being
    silently treated as a filename."""
    spec = (spec or "").strip()
    m = _RANGE_RE.match(spec)
    if m:
        path_part, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        if start < 1 or start > end:  # 1-indexed; START>END is nonsense
            return Reason.BAD_RANGE
        return path_part, start, end
    head, sep, tail = spec.rpartition(":")
    if sep and head and _ATTEMPT_RE.match(tail) and any(c.isdigit() for c in tail):
        return Reason.BAD_RANGE  # `foo.py:10`, `foo.py:10-`, `foo.py:-5`
    return spec, None, None


def _is_blocked(rel_path: str, extra: tuple[str, ...]) -> bool:
    """Component-anchored blocklist on the ROOT-RELATIVE resolved path. Defaults:
    any ``.git`` path component, and any ``.env`` / ``.env.*`` basename. ``extra``
    tokens (from config) match either a path component or the basename. Anchored on
    components — never substring — so ``.github/`` and ``.gitignore`` stay allowed."""
    parts = Path(rel_path).parts
    base = Path(rel_path).name
    if any(p == ".git" for p in parts):
        return True
    if base == ".env" or base.startswith(".env."):
        return True
    for tok in extra:
        if tok and (tok in parts or base == tok):
            return True
    return False


def resolve_within(
    root: str, spec: str, *, extra_blocklist: tuple[str, ...] = ()
) -> Resolved | Reason:
    """Validate ``spec`` against ``root`` and return a ``Resolved`` or a typed
    ``Reason``. Never raises. ``root`` is realpath'd here too (defensively) so a
    symlinked root can't fail containment for legitimate files."""
    parsed = _parse_spec(spec)
    if isinstance(parsed, Reason):
        return parsed
    path_part, start, end = parsed
    if not path_part.strip():
        return Reason.NOT_FILE
    try:
        real_root = os.path.realpath(root)
        # Join with the spec path; an absolute spec discards root and then fails
        # containment below — which is exactly what we want.
        target = os.path.realpath(os.path.join(real_root, path_part))
        contained = os.path.commonpath([real_root, target]) == real_root
    except (OSError, ValueError):
        # ValueError: mixed drives / relative-vs-absolute on Windows. Fail closed.
        return Reason.ESCAPE
    if not contained:
        return Reason.ESCAPE
    rel = os.path.relpath(target, real_root)
    if _is_blocked(rel, extra_blocklist):
        return Reason.BLOCKED
    if not os.path.isfile(target):
        return Reason.NOT_FILE
    return Resolved(abs_path=target, rel_path=rel, start=start, end=end)


def read_content(resolved: Resolved, *, max_bytes: int) -> tuple[str, int] | Reason:
    """Read a resolved target's text and byte count, or a typed ``Reason``.

    Whole file: rejected ``TOO_LARGE`` (via ``fstat``) before its bytes are read.
    Range: read incrementally and stop after ``end``; the cap applies to the bytes
    actually COLLECTED, so a small slice of a huge file succeeds. A NUL byte in the
    collected bytes marks it ``BINARY``; otherwise decode UTF-8 with ``errors=replace``
    so a stray odd byte never raises."""
    try:
        with open(resolved.abs_path, "rb") as fh:
            st = os.fstat(fh.fileno())
            if resolved.start is None:
                if st.st_size > max_bytes:
                    return Reason.TOO_LARGE
                data = fh.read()
                if b"\x00" in data:
                    return Reason.BINARY
                return data.decode("utf-8", "replace"), len(data)
            # Range: iterate lines, collect [start, end], bail past end.
            collected = bytearray()
            lineno = 0
            for raw in fh:
                lineno += 1
                if lineno > resolved.end:
                    break
                if lineno >= resolved.start:
                    collected += raw
                    if len(collected) > max_bytes:
                        return Reason.TOO_LARGE
            if lineno < resolved.start:  # START past EOF (END was clamped implicitly)
                return Reason.BAD_RANGE
            if b"\x00" in collected:
                return Reason.BINARY
            return collected.decode("utf-8", "replace"), len(collected)
    except OSError:
        return Reason.UNREADABLE
