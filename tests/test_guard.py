"""ask_fable request guard — sanity floor + denylist delegation, ordering.

The denylist (`check_denylist`) is stubbed so these tests don't depend on
the bundled list's contents.
"""

from __future__ import annotations

import ask_fable.guard as guard


def test_denylist_rejection_uses_label(monkeypatch):
    monkeypatch.setattr(guard, "check_denylist", lambda p: (False, "offensive-security content"))
    allowed, reason = guard.check("a perfectly long and specific enough question here")
    assert allowed is False
    assert reason == "offensive-security content"


def test_allowed_when_denylist_passes(monkeypatch):
    monkeypatch.setattr(guard, "check_denylist", lambda p: (True, ""))
    allowed, reason = guard.check("Does run() call _load_graph before _maybe_reload in serve.py?")
    assert allowed is True and reason == ""


def test_empty_and_near_empty_rejected(monkeypatch):
    monkeypatch.setattr(guard, "check_denylist", lambda p: (True, ""))
    assert guard.check("")[0] is False
    assert guard.check("   ")[0] is False
    assert guard.check("hi")[1].startswith("question too short")  # 2 < 3 chars


def test_max_len_measured_on_stripped_question(monkeypatch):
    # Both length checks use the stripped question: surrounding whitespace must
    # not push an otherwise-valid question over the cap.
    monkeypatch.setattr(guard, "check_denylist", lambda p: (True, ""))
    monkeypatch.setenv("ASK_FABLE_MAX_LEN", "100")
    q = "x" * 100  # exactly at the cap
    assert guard.check(q) == (True, "")
    assert guard.check("   " + q + "   ") == (True, "")  # padding is not counted
    assert guard.check("x" * 101)[1].startswith("question too long")


def test_breadth_is_allowed(monkeypatch):
    monkeypatch.setattr(guard, "check_denylist", lambda p: (True, ""))
    assert guard.check("how should I structure this service?")[0] is True
    assert guard.check("tell me everything about this module")[0] is True


def test_too_long_and_context_cap(monkeypatch):
    monkeypatch.setattr(guard, "check_denylist", lambda p: (True, ""))
    monkeypatch.setenv("ASK_FABLE_MAX_LEN", "4000")  # pin cap; assert the mechanism, not the default
    assert guard.check("word " * 3000)[1].startswith("question too long")
    ok = "How does the request router dispatch to handlers here?"
    # Context is UNBOUNDED by default — huge context is allowed.
    assert guard.check(ok, context="x" * 1_000_000) == (True, "")
    # A small configured cap is floored to CONTEXT_MIN (>=512k), so 20k still passes.
    monkeypatch.setenv("ASK_FABLE_MAX_CONTEXT_LEN", "20000")
    assert guard.check(ok, context="x" * 20001) == (True, "")
    assert guard.check(ok, context="x" * (guard.CONTEXT_MIN + 1))[1].startswith("context too large")


# ── trusted_session flag ────────────────────────────────────────────────

def test_trusted_session_allows_blocked_content(monkeypatch):
    """trusted=true: denylist hit is logged but does not block — question proceeds."""
    monkeypatch.setattr(guard, "check_denylist", lambda p: (False, "offensive-security content"))
    allowed, reason = guard.check("analyze this PoC exploit for CVE-2024-12345", trusted=True)
    assert allowed is True
    assert reason != ""  # label is returned for audit


def test_trusted_session_passes_clean_questions_normally(monkeypatch):
    """trusted=true has no effect on clean questions — they pass as normal."""
    monkeypatch.setattr(guard, "check_denylist", lambda p: (True, ""))
    allowed, reason = guard.check("how does the ELF loader work?", trusted=True)
    assert allowed is True and reason == ""


def test_trusted_false_blocks_denylist_hit(monkeypatch):
    """trusted not set (default False): denylist hit blocks."""
    monkeypatch.setattr(guard, "check_denylist", lambda p: (False, "offensive-security content"))
    allowed, reason = guard.check("analyze this PoC exploit")
    assert allowed is False
    assert "trusted" not in reason.lower()


def test_trusted_session_still_runs_sanity_checks(monkeypatch):
    """trusted=true does not bypass the sanity floor — empty questions still refused."""
    monkeypatch.setattr(guard, "check_denylist", lambda p: (True, ""))
    allowed, _ = guard.check("", trusted=True)
    assert allowed is False  # empty question fails sanity floor regardless


def test_trusted_session_respects_denylist_label(monkeypatch):
    """Trusted mode returns the matched denylist label so callers can audit."""
    monkeypatch.setattr(guard, "check_denylist", lambda p: (False, "biology dual-use content"))
    allowed, reason = guard.check("engineer a viral vector", trusted=True)
    assert allowed is True
    assert "biology dual-use content" in reason
