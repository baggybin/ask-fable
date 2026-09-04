"""Audit log: pre-create 0o600, fcntl.flock, fsync, size-based rotation."""

from __future__ import annotations

import json
import sys

import ask_fable.audit as audit


def test_file_created_with_0o600(monkeypatch, tmp_path):
    """First write must create the file with mode 0o600 — no world-readable window."""
    p = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(p))
    audit.record(decision="allowed", stage=None, reason="ok", question="q1", session="s1")
    assert p.exists()
    if sys.platform != "win32":
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_records_are_valid_jsonl(monkeypatch, tmp_path):
    p = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(p))
    for i in range(3):
        audit.record(decision="allowed", stage=None, reason="ok",
                      question=f"q{i}", session=f"s{i}")
    lines = p.read_text().strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        rec = json.loads(line)
        assert "ts" in rec
        assert "event_id" in rec
        assert "question_sha256" in rec


def test_raw_mode_keeps_raw_question(monkeypatch, tmp_path):
    p = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(p))
    monkeypatch.setenv("ASK_FABLE_AUDIT_RAW", "1")
    audit.record(decision="allowed", stage=None, reason="ok", question="secret question")
    rec = json.loads(p.read_text().strip())
    assert rec["question_raw"] == "secret question"
    assert rec["context_raw"] == ""


def test_rotation_at_max_bytes(monkeypatch, tmp_path):
    p = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(p))
    monkeypatch.setenv("ASK_FABLE_AUDIT_MAX_BYTES", "200")  # very small cap
    monkeypatch.setenv("ASK_FABLE_AUDIT_BACKUPS", "2")
    # Each record is well over 100 bytes because of the sha256 + uuid + raw.
    for i in range(8):
        audit.record(decision="allowed", stage=None, reason="ok",
                      question=f"question-{i}-" + "x" * 50, session="s1")
    assert p.exists()
    rotated = list(tmp_path.glob("decisions.*.jsonl"))
    assert len(rotated) == 2
    for segment in rotated:
        for line in segment.read_text().strip().split("\n"):
            json.loads(line)


def test_zero_backups_truncates(monkeypatch, tmp_path):
    """ASK_FABLE_AUDIT_BACKUPS=0 discards old content rather than keeping it."""
    p = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(p))
    monkeypatch.setenv("ASK_FABLE_AUDIT_MAX_BYTES", "100")
    monkeypatch.setenv("ASK_FABLE_AUDIT_BACKUPS", "0")
    for i in range(5):
        audit.record(decision="allowed", stage=None, reason="ok",
                      question=f"q-{i}-" + "y" * 50)
    assert p.exists()
    assert p.read_text().count("\n") <= 1
    assert not list(tmp_path.glob("decisions.*.jsonl"))


def test_never_raises_on_bad_path(monkeypatch, tmp_path):
    """A misconfigured path must not raise; just no-op."""
    # Path under a non-existent parent + can't be created
    bad = tmp_path / "nonexistent" / "deeper" / "still" / "log"
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(bad))
    # Should not raise
    audit.record(decision="allowed", stage=None, reason="ok", question="q")
