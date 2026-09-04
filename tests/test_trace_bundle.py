from __future__ import annotations

import json

from ask_fable import trace_bundle


def test_full_bundle_redacts_and_caps_content(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_TRACE_MODE", "full")
    monkeypatch.setenv("ASK_FABLE_TRACE_DIR", str(tmp_path))
    monkeypatch.setenv("ASK_FABLE_TRACE_MAX_CONTENT_BYTES", "80")
    path = trace_bundle.write("trace-1", {"token": "secret-value", "question": "x" * 200})
    assert path is not None
    payload = json.loads(path.read_text())
    assert "secret-value" not in path.read_text()
    assert payload["truncated"] is True


def test_safe_mode_does_not_write_bundle(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_TRACE_MODE", "safe")
    monkeypatch.setenv("ASK_FABLE_TRACE_DIR", str(tmp_path))
    assert trace_bundle.write("trace-1", {"question": "q"}) is None


def test_bundle_rejects_symlink_target(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_TRACE_MODE", "full")
    monkeypatch.setenv("ASK_FABLE_TRACE_DIR", str(tmp_path))
    victim = tmp_path / "victim"
    victim.write_text("safe")
    (tmp_path / "trace-1.json").symlink_to(victim)
    assert trace_bundle.write("trace-1", {"question": "q"}) is None
    assert trace_bundle.read("trace-1", 100) is None
    assert victim.read_text() == "safe"
