"""Tests for the answer-persistence layer (outputs.save)."""

import importlib
import os

import pytest

import ask_fable.outputs as outputs


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ASK_FABLE_SAVE", "1")
    monkeypatch.setenv("ASK_FABLE_OUTPUT_DIR", str(tmp_path))
    importlib.reload(outputs)
    return tmp_path


def test_safe_mode_does_not_save_raw_content_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ASK_FABLE_SAVE", raising=False)
    monkeypatch.setenv("ASK_FABLE_TRACE_MODE", "safe")
    monkeypatch.setenv("ASK_FABLE_OUTPUT_DIR", str(tmp_path))
    assert outputs.save(tool="ask", model="m", question="secret", answer="answer") is None


def test_save_writes_markdown(out_dir):
    path = outputs.save(
        tool="ask",
        model="claude-fable-5",
        question="Why?",
        answer="Because.",
        context="ctx",
        session="default",
    )
    assert path is not None
    p = out_dir / path.split("/")[-1]
    text = p.read_text()
    assert "# ask_fable answer — ask" in text
    assert "Why?" in text and "Because." in text and "claude-fable-5" in text
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_save_includes_sources(out_dir):
    path = outputs.save(
        tool="ask_council",
        model="claude-fable-5",
        question="Q",
        answer="merged",
        sources=[
            {"status": "ok", "model": "fable", "answer": "a1"},
            {"status": "error", "model": "glm", "kind": "timeout", "detail": "slow"},
        ],
    )
    text = (out_dir / path.split("/")[-1]).read_text()
    assert "## Sources" in text and "### fable" in text and "a1" in text
    assert "### glm — error" in text


def test_save_includes_thinking(out_dir):
    path = outputs.save(
        tool="ask",
        model="claude-fable-5",
        question="Why?",
        answer="Because.",
        thinking="step 1: consider X\nstep 2: rule out Y",
    )
    text = (out_dir / path.split("/")[-1]).read_text()
    assert "## Thinking" in text
    assert "step 1: consider X" in text and "step 2: rule out Y" in text


def test_save_includes_per_source_thinking(out_dir):
    path = outputs.save(
        tool="ask_council",
        model="claude-fable-5",
        question="Q",
        answer="merged",
        thinking="synth reasoning here",
        sources=[
            {"status": "ok", "model": "fable", "answer": "a1", "thinking": "fable trace"},
            {"status": "ok", "model": "glm", "answer": "a2"},  # no trace -> no section
        ],
    )
    text = (out_dir / path.split("/")[-1]).read_text()
    assert "## Thinking" in text and "synth reasoning here" in text
    assert "#### fable — thinking" in text and "fable trace" in text
    assert "#### glm — thinking" not in text  # absent trace renders nothing


def test_disabled_returns_none(out_dir, monkeypatch):
    monkeypatch.setenv("ASK_FABLE_SAVE", "0")
    importlib.reload(outputs)
    assert outputs.save(tool="ask", model="m", question="q", answer="a") is None
    assert list(out_dir.iterdir()) == []


def test_retention_prunes_oldest(out_dir, monkeypatch):
    monkeypatch.setenv("ASK_FABLE_MAX_ANSWERS", "3")
    importlib.reload(outputs)
    for i in range(5):
        outputs.save(tool="ask", model="m", question=f"q{i}", answer=f"a{i}")
    files = sorted(out_dir.glob("*.md"))
    assert len(files) == 3


def test_retention_never_deletes_foreign_markdown(out_dir, monkeypatch):
    """A user-pointed ASK_FABLE_OUTPUT_DIR may hold the user's own .md files;
    the retention cap must only ever prune answers save() itself wrote."""
    monkeypatch.setenv("ASK_FABLE_MAX_ANSWERS", "1")
    importlib.reload(outputs)
    notes = out_dir / "my-notes.md"
    notes.write_text("precious")
    os.utime(notes, (0, 0))  # oldest file in the dir by far
    for i in range(3):
        outputs.save(tool="ask", model="m", question=f"q{i}", answer=f"a{i}")
    assert notes.exists() and notes.read_text() == "precious"
    # One owned answer retained plus the untouched foreign file.
    assert len(list(out_dir.glob("*.md"))) == 2


def test_every_markdown_section_is_redacted(out_dir):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.signaturevalue"
    path = outputs.save(
        tool="ask_council",
        model="m",
        question="Cookie: secret-cookie",
        answer=jwt,
        context="https://alice:uri-canary@example.test/x",
        thinking="token: think-canary",
        sources=[
            {"status": "ok", "model": "x", "answer": jwt, "thinking": "password=source-canary"}
        ],
    )
    text = (out_dir / path.split("/")[-1]).read_text()
    assert "secret-cookie" not in text
    assert "uri-canary" not in text
    assert "eyJhbGci" not in text
    assert "think-canary" not in text
    assert "source-canary" not in text
