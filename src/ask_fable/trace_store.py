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

from .telemetry import JSONValue, TraceEvent


@dataclass(frozen=True, slots=True)
class StoredRecord:
    schema_version: int
    payload: Mapping[str, JSONValue]


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

    def iter_records(self) -> Iterator[StoredRecord]:
        self.unsupported_versions = 0
        self.truncated_lines = 0
        paths = sorted({
            *self.path.parent.glob(f"{self.path.stem}.*{self.path.suffix}"),
            *self.path.parent.glob(f"{self.path.name}.*"),
        })
        if self.path.exists() and self.path.is_file():
            paths.append(self.path)
        for path in paths:
            yield from self._read_path(path)

    def _read_path(self, path: Path) -> Iterator[StoredRecord]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                return
        except (OSError, UnicodeError):
            return
        with os.fdopen(fd, "rb") as source:
            limit = self._max_line_bytes()
            while raw := source.readline(limit + 1):
                if len(raw) > limit and not raw.endswith(b"\n"):
                    while raw and not raw.endswith(b"\n"):
                        raw = source.readline(limit + 1)
                    continue
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
