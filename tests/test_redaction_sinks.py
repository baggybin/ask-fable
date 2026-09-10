"""Bug hunt 2026-09-09 — close the redaction-coverage holes (TR-L2, F2, TR-H1).

Fixtures are built by concatenation (`_k`) so no contiguous key-shaped literal sits
in the source — the joined value still exercises the redaction regexes, but GitHub
secret-scanning push protection (public mirror) sees no secret to block.
"""

from __future__ import annotations

import ask_fable.hub as hub
import ask_fable.trace_runtime as trace_runtime
from ask_fable.redaction import redact_text


def _k(prefix: str, body: str) -> str:
    return prefix + body


# --- TR-L2: bare, unlabeled provider keys -------------------------------------

def test_bare_provider_keys_are_redacted():
    for key in (
        _k("sk-ant-", "abc123DEF456ghi789JKL012mno345pqr678"),
        _k("sk-", "abcdefghij0123456789ABCDEF"),
        _k("sk-proj-", "abcdefghij0123456789ABCDEF"),
        _k("AIza", "SyD0123456789abcdefghijklmnopqrs"),
        _k("xoxb-", "1234567890-abcdefghijklmnop"),
    ):
        out, n = redact_text(f"the key is {key} ok")
        assert key not in out and "[REDACTED]" in out and n >= 1


def test_sk_proj_key_with_internal_separators_fully_redacted():
    # a real sk-proj- key contains -/_ in its body; the whole thing must go, not just
    # the leading alnum run (verifier catch: the body used to exclude separators)
    key = _k("sk-proj-", "Ab12_cd34-Ef56gh78IJ90klMN-pqRS")
    out, n = redact_text(f"error: invalid api key {key}")
    assert key not in out and "pqRS" not in out and "[REDACTED]" in out and n >= 1


def test_redaction_does_not_eat_ordinary_hyphenated_words():
    # prefix-anchored patterns must not fire on normal prose/code
    for benign in ("risk-averse tradeoffs", "the task-force met", "a sk-based idea"):
        out, n = redact_text(benign)
        assert out == benign and n == 0


# --- F2: trace error block (provider-controlled detail) -----------------------

def test_error_block_detail_is_redacted():
    key = _k("sk-ant-", "abc123DEF456ghi789JKL012mno345pqr678")
    block = trace_runtime._error_from_payload(
        {"kind": "http_error", "detail": f"401 from provider: invalid api key {key}"},
        "error",
    )
    assert block is not None
    assert "sk-ant-" not in block["detail"] and "[REDACTED]" in block["detail"]


# --- TR-H1: hub stores redacted question/answer -------------------------------

def test_hub_redacts_question_and_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_HUB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.delenv("ASK_FABLE_HUB", raising=False)
    hub._sweep_counter = 0
    secret_answer = "Authorization: Bearer " + "hunter2notarealvalue"
    hub.write_turn(
        agent_id="a1", project="p1", session_key="s1",
        question="my token is " + _k("sk-ant-", "abc123DEF456ghi789JKL012mno345pqr678"),
        answer=secret_answer,
        oracle="claude-fable-5", status="ok",
    )
    peek = hub.peek_session(session_key="s1")
    turn = peek["turns"][0]
    assert "sk-ant-" not in turn["question"] and "[REDACTED]" in turn["question"]
    assert "hunter2notarealvalue" not in turn["answer"] and "[REDACTED]" in turn["answer"]
