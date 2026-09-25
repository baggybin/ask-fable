from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .telemetry import JSONValue, TraceEvent

_BACKWARD_BLOCK = 65_536  # bytes per read when scanning a file last line first


@dataclass(frozen=True, slots=True)
class StoredRecord:
    schema_version: int
    payload: Mapping[str, JSONValue]


def _lines_forward(source: BinaryIO, limit: int) -> Iterator[bytes]:
    """A file's lines in order; one over ``limit`` bytes is skipped, never buffered."""
    while raw := source.readline(limit + 1):
        if len(raw) > limit and not raw.endswith(b"\n"):
            while raw and not raw.endswith(b"\n"):
                raw = source.readline(limit + 1)
            continue
        yield raw


def _lines_backwards(source: BinaryIO, limit: int) -> Iterator[bytes]:
    """Yield a file's lines LAST first, each with its ``\\n`` (the final line may lack one).

    Reads fixed blocks from the end, so memory stays bounded by one block plus one line; a
    line over ``limit`` bytes is skipped without ever being buffered whole, just as the
    bounded forward ``readline`` skips it."""
    pos = source.seek(0, os.SEEK_END)
    # The line being assembled: its end is known, its start lies in a block not yet read.
    # Only the file's final line can be unterminated, so `ended` flips once a "\n" is seen.
    carry, dropped, ended = b"", False, False
    while pos > 0:
        step = min(_BACKWARD_BLOCK, pos)
        pos -= step
        source.seek(pos)
        pieces = source.read(step).split(b"\n")
        if len(pieces) == 1:  # no line break in this block: all of it extends `carry`
            if not dropped:
                carry = pieces[0] + carry
                dropped = len(carry) > limit
                carry = b"" if dropped else carry
            continue
        # pieces[-1] completes the carried line; the inner pieces are whole lines; pieces[0]
        # starts the next carried line (it ends at a "\n", its start is further back).
        done = [(pieces[-1] + carry, ended, dropped)]
        done += [(piece, True, False) for piece in reversed(pieces[1:-1])]
        for content, has_newline, skip in done:
            if not skip and len(content) <= limit and (content or has_newline):
                yield content + b"\n" if has_newline else content
        carry, ended = pieces[0], True
        dropped = len(carry) > limit
        carry = b"" if dropped else carry
    if not dropped and (carry or ended):
        yield carry + b"\n" if ended else carry


class EventStore:
    def __init__(
        self,
        *,
        path: Path,
        max_bytes: int = 52_428_800,
        retain_segments: int | None = None,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.retain_segments = retain_segments
        self.unsupported_versions = 0
        self.truncated_lines = 0

    @staticmethod
    def _max_line_bytes() -> int:
        try:
            return max(1024, int(os.environ.get("ASK_FABLE_TRACE_MAX_EVENT_BYTES") or 1_048_576))
        except ValueError:
            return 1_048_576

    def append(self, event: TraceEvent | Mapping[str, JSONValue]) -> bool:
        candidate = event.trace_id if isinstance(event, TraceEvent) else event.get("trace_id")
        trace_id = candidate if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", candidate) else "unknown"
        try:
            payload = event.to_dict() if isinstance(event, TraceEvent) else dict(event)
            line = json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ) + "\n"
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if lock_path.exists() or lock_path.is_symlink():
                mode = lock_path.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    return False
            os.chmod(self.path.parent, 0o700)
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(lock_fd, "r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                os.chmod(lock_path, 0o600)
                if self.path.exists() or self.path.is_symlink():
                    mode = self.path.lstat().st_mode
                    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                        return False
                self._rotate_if_needed(len(line.encode("utf-8")))
                fd = os.open(
                    self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "a", encoding="utf-8") as output:
                    os.chmod(self.path, 0o600)
                    output.write(line)
                    output.flush()
                    os.fsync(output.fileno())
                self._fsync_directory()
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return True
        except (OSError, TypeError, ValueError):
            print(f"ask_fable: trace write failed: trace_id={trace_id} category=storage_error", file=sys.stderr)
            return False

    def iter_records(self, *, newest_first: bool = False) -> Iterator[StoredRecord]:
        """Every stored record, rotated segments first. ``newest_first`` reverses it — the
        active file read last line first, then each rotated file newest to oldest — so a
        caller that caps its scan drops the OLDEST records rather than the newest."""
        self.unsupported_versions = 0
        self.truncated_lines = 0
        stamped = set(self.path.parent.glob(f"{self.path.stem}.*{self.path.suffix}"))
        legacy = set(self.path.parent.glob(f"{self.path.name}.*")) - stamped
        active = [self.path] if self.path.exists() and self.path.is_file() else []
        if not newest_first:
            for path in [*sorted(stamped | legacy), *active]:
                yield from self._read_path(path)
            return
        # Timestamped segments sort by age; legacy numbered generations (`.1` newest) are
        # older than any of them.
        rotated = [*sorted(stamped, reverse=True), *sorted(legacy, key=self._generation)]
        for path in [*active, *rotated]:
            yield from self._read_path(path, backwards=True)

    def _generation(self, path: Path) -> tuple[int, str]:
        tail = path.name[len(self.path.name) + 1:]
        return (int(tail), "") if tail.isdigit() else (sys.maxsize, tail)

    def _read_path(self, path: Path, *, backwards: bool = False) -> Iterator[StoredRecord]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                return
        except (OSError, UnicodeError):
            return
        with os.fdopen(fd, "rb") as source:
            limit = self._max_line_bytes()
            read = _lines_backwards if backwards else _lines_forward
            for raw in read(source, limit):
                try:
                    line = raw.decode("utf-8")
                    value = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    if not raw.endswith(b"\n"):
                        self.truncated_lines += 1
                    continue
                if not isinstance(value, dict):
                    continue
                version = value.get("schema_version", 1)
                if version not in (1, 2):
                    self.unsupported_versions += 1
                    continue
                yield StoredRecord(schema_version=version, payload=value)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if self.max_bytes <= 0 or not self.path.exists():
            return
        if self.path.stat().st_size + incoming_bytes <= self.max_bytes:
            return
        if self.retain_segments == 0:
            self.path.unlink()
            self._fsync_directory()
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        sequence = 0
        while True:
            segment = self.path.with_name(f"{self.path.stem}.{stamp}.{sequence:06d}{self.path.suffix}")
            if not segment.exists():
                self.path.replace(segment)
                os.chmod(segment, 0o600)
                self._trim_segments()
                self._fsync_directory()
                return
            sequence += 1

    def _trim_segments(self) -> None:
        if self.retain_segments is None:
            return
        segments = sorted(self.path.parent.glob(f"{self.path.stem}.*{self.path.suffix}"))
        for segment in segments[: -self.retain_segments]:
            segment.unlink()

    def _fsync_directory(self) -> None:
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
