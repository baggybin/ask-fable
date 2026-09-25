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


# --- token prose vs token secrets ----------------------------------------------
# Answers ABOUT tokens are ordinary engineering prose; a token assignment is only
# redacted when its value looks like a credential.

def test_token_prose_survives_redaction():
    for benign in (
        "returns `token = 42` from the lock service",
        "token = self.seq",
        "the token: a monotonic counter",
        "refresh_token = current",
    ):
        out, n = redact_text(benign)
        assert out == benign and n == 0, benign


def test_token_secret_values_are_still_redacted():
    out, n = redact_text("token = abcdef0123456789ABCDEF")
    assert "abcdef0123456789ABCDEF" not in out and "[REDACTED]" in out and n == 1
    out, n = redact_text("refresh_token: 'aZ09xY8wVu7tSr6q'")
    assert "aZ09xY8wVu7tSr6q" not in out and "[REDACTED]" in out and n == 1


def test_redacted_token_value_preserves_the_rest_of_the_line():
    out, n = redact_text("token = abcdef0123456789ABCDEF and then more prose")
    assert out == "token = [REDACTED] and then more prose" and n == 1


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


# --- bug hunt 2026-09-25: common secret shapes that used to pass through -------

def test_quoted_key_secrets_are_redacted_word_by_word():
    for src, leaked in (
        ('{"access_token": "abcd1234efgh5678ijkl", "user": "bob"}', "abcd1234efgh5678ijkl"),
        ("{'refresh_token': 'zz9zz9zz9', 'scope': 'read'}", "zz9zz9zz9"),
        ('{"clientSecret":"s3cr3t-val","apiKey":"k3y-val"}', "s3cr3t-val"),
        ('{"db_password": "hunter2hunter2"}', "hunter2hunter2"),
        ('"password" => "hunter2hunter2"', "hunter2hunter2"),
    ):
        out, n = redact_text(src)
        assert leaked not in out and "[REDACTED]" in out and n >= 1, src
    # the rest of the object survives, and non-secret keys stay readable
    out, _ = redact_text('{"access_token": "abcd1234efgh5678ijkl", "user": "bob"}')
    assert '"user": "bob"' in out


def test_quoted_non_secret_keys_are_left_alone():
    src = '{"max_tokens": "4096", "tokenizer": "cl100k", "secretary": "ann"}'
    assert redact_text(src) == (src, 0)


def test_env_style_secret_assignments_are_redacted():
    for src, leaked in (
        ("aws_secret_access_key = wJalrXUtnFEMIK7MDENGbPxRfiCY", "wJalrXUtnFEMIK7MDENGbPxRfiCY"),
        ("PASSWD=hunter2hunter2", "hunter2hunter2"),
        ('private_key = "abc-def-ghi"', "abc-def-ghi"),
        ("STRIPE_SECRET_KEY=whatever-value", "whatever-value"),
    ):
        out, n = redact_text(src)
        assert leaked not in out and n == 1, src
    assert redact_text("the secretly: named thing") == ("the secretly: named thing", 0)


def test_bare_stripe_keys_are_redacted():
    for key in (_k("sk_live_", "abcdefghijklmnop1234"), _k("rk_test_", "ABCDEFGHIJKLMNOP1234")):
        out, n = redact_text(f"found {key} in logs")
        assert key not in out and "[REDACTED]" in out and n == 1
