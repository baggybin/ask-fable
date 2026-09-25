from __future__ import annotations

import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor

from ask_fable import trace_store
from ask_fable.telemetry import TraceEvent
from ask_fable.trace_store import EventStore


def _event(index: int) -> TraceEvent:
    return TraceEvent.new(event_name="test.event", kind="internal", status=str(index))


def test_append_and_iterate_v2_and_legacy_v1(tmp_path) -> None:
    # Given
    path = tmp_path / "decisions.jsonl"
    path.write_text(json.dumps({"event_id": "legacy", "decision": "allowed"}) + "\n")
    store = EventStore(path=path)

    # When
    assert store.append(_event(1))
    result = list(store.iter_records())

    # Then
    assert [record.schema_version for record in result] == [1, 2]


def test_iteration_skips_unsupported_and_tolerates_truncated_tail(tmp_path) -> None:
    # Given
    path = tmp_path / "decisions.jsonl"
    path.write_text('{"schema_version":99}\n{"schema_version":2')
    store = EventStore(path=path)

    # When
    result = list(store.iter_records())

    # Then
    assert result == []
    assert store.unsupported_versions == 1
    assert store.truncated_lines == 1


def test_newest_first_reads_every_file_backwards(tmp_path, monkeypatch) -> None:
    # Given: a legacy numbered generation, two stamped segments and the active file, read
    # in 7-byte blocks so every line straddles block boundaries
    monkeypatch.setattr(trace_store, "_BACKWARD_BLOCK", 7)

    def lines(*numbers: int) -> str:
        return "".join(json.dumps({"schema_version": 2, "n": n}) + "\n" for n in numbers)

    (tmp_path / "decisions.jsonl.1").write_text(lines(1, 2))
    (tmp_path / "decisions.20260101T000000000000Z.000000.jsonl").write_text(lines(3, 4))
    (tmp_path / "decisions.20260102T000000000000Z.000000.jsonl").write_text(lines(5, 6))
    path = tmp_path / "decisions.jsonl"
    path.write_text(lines(7, 8) + '{"schema_version":99}\n{"schema_version":2')
    store = EventStore(path=path)

    # When
    result = [record.payload["n"] for record in store.iter_records(newest_first=True)]

    # Then: newest record first, and the same skip/truncation accounting as a forward read
    assert result == [8, 7, 6, 5, 4, 3, 2, 1]
    assert store.unsupported_versions == 1
    assert store.truncated_lines == 1


def test_newest_first_skips_oversized_lines_like_a_forward_read(tmp_path, monkeypatch) -> None:
    # Given: an over-limit line between two good ones, and a tiny read block
    monkeypatch.setenv("ASK_FABLE_TRACE_MAX_EVENT_BYTES", "1024")
    monkeypatch.setattr(trace_store, "_BACKWARD_BLOCK", 64)
    path = tmp_path / "decisions.jsonl"
    big = json.dumps({"schema_version": 2, "n": 2, "pad": "x" * 2000})
    path.write_text(
        json.dumps({"schema_version": 2, "n": 1}) + "\n" + big + "\n"
        + json.dumps({"schema_version": 2, "n": 3}) + "\n"
    )
    store = EventStore(path=path)

    # When / Then
    forward = [record.payload["n"] for record in store.iter_records()]
    backward = [record.payload["n"] for record in store.iter_records(newest_first=True)]
    assert forward == [1, 3] and backward == [3, 1]


def test_rotation_is_immutable_and_concurrent_appends_are_complete(tmp_path) -> None:
    # Given
    store = EventStore(path=tmp_path / "decisions.jsonl", max_bytes=350)

    # When
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda index: store.append(_event(index)), range(40)))

    # Then
    assert all(outcomes)
    assert len(list(store.iter_records())) == 40
    segments = list(tmp_path.glob("decisions.*.jsonl"))
    assert segments
    assert len({path.name for path in segments}) == len(segments)


def test_append_is_fail_open_on_write_failure(tmp_path) -> None:
    # Given
    path = tmp_path / "directory-not-file"
    path.mkdir()
    store = EventStore(path=path)

    # When / Then
    assert store.append(_event(1)) is False


def test_append_repairs_permissions_on_existing_files(tmp_path) -> None:
    # Given
    path = tmp_path / "decisions.jsonl"
    lock_path = tmp_path / "decisions.jsonl.lock"
    path.write_text("")
    lock_path.write_text("")
    path.chmod(0o666)
    lock_path.chmod(0o666)
    tmp_path.chmod(0o755)

    # When
    EventStore(path=path).append(_event(1))

    # Then
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
        assert lock_path.stat().st_mode & 0o777 == 0o600
        assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_finite_retention_keeps_requested_number_of_immutable_segments(tmp_path) -> None:
    # Given
    store = EventStore(path=tmp_path / "decisions.jsonl", max_bytes=1, retain_segments=2)

    # When
    for index in range(5):
        assert store.append(_event(index))

    # Then
    assert len(list(tmp_path.glob("decisions.*.jsonl"))) == 2


def test_zero_retention_discards_rotated_content(tmp_path) -> None:
    # Given
    store = EventStore(path=tmp_path / "decisions.jsonl", max_bytes=1, retain_segments=0)

    # When
    for index in range(3):
        assert store.append(_event(index))

    # Then
    assert not list(tmp_path.glob("decisions.*.jsonl"))
    assert len(list(store.iter_records())) == 1


def test_append_fails_open_for_non_json_numeric_values(tmp_path) -> None:
    # Given
    store = EventStore(path=tmp_path / "decisions.jsonl")

    # When
    outcome = store.append({"schema_version": 2, "duration_ms": math.nan})

    # Then
    assert outcome is False
    assert not store.path.exists()


def test_append_rejects_symlink_audit_target(tmp_path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("safe")
    path = tmp_path / "events.jsonl"
    path.symlink_to(victim)
    assert EventStore(path=path).append(_event(1)) is False
    assert victim.read_text() == "safe"
