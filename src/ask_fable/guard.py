"""Two-layer request gate for ask_fable.

Layer 1 is a cheap non-empty/size SANITY floor (NOT a breadth filter — broad
engineering questions are allowed). Layer 2 is the prohibited-use denylist
(``_denylist``): a bundled pattern list that covers offensive-security and
biology dual-use terms, with operator-extendable allowlist/denylist files.
The model scope contract (``prompts.FABLE_SYSTEM_PROMPT``) is the third layer
that decides semantic scope — allowing broad engineering questions and refusing
cyber/attack content and non-software domains (biology restricted; computational
domains — neuroscience, cognitive science, AI/ML, computer science — are
in-scope).

ask_fable intentionally uses its OWN bundled denylist, not salient-core's.
The bundled list carries both offensive-security and biology dual-use terms
that salient-core's kernel (being a public, verification-free kernel) does not.
The two projects couple loosely: salient-core can import ask_fable's guard at
runtime if it wants the richer list, but the reverse is never true.

Order matters: the denylist passes empty input through, so the sanity floor runs
first.
"""

from __future__ import annotations

import os

from ._denylist import check_denylist

# Minimum context capacity, in characters. Even when a cap is explicitly
# configured it is never lowered below this — context is always at least ~512k.
CONTEXT_MIN = 512_000


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def check(question: str, context: str = "", *, trusted: bool = False) -> tuple[bool, str]:
    """Return (allowed, reason). ``reason`` is "" when allowed. No model call.

    When ``trusted`` is True, the prohibited-use denylist runs in log-only mode:
    it still returns the matched label (so the caller can audit) but never blocks.
    This is the operator-authorized override for legitimate security-engineering
    questions that genuinely need security vocabulary (e.g. "analyze this PoC
    exploit for CVE-2024-12345").
    """
    q = (question or "").strip()
    min_len = _int_env("ASK_FABLE_MIN_LEN", 3)
    max_len = _int_env("ASK_FABLE_MAX_LEN", 65536)
    # Context is UNBOUNDED by default (0 = off). Set ASK_FABLE_MAX_CONTEXT_LEN>0
    # to re-impose a cap; the question-length cap still guards against runaways.
    # Any cap is floored to CONTEXT_MIN — context capacity is always >= 512k chars.
    max_ctx = _int_env("ASK_FABLE_MAX_CONTEXT_LEN", 0)
    if 0 < max_ctx < CONTEXT_MIN:
        max_ctx = CONTEXT_MIN

    # Layer 1 — non-empty + size sanity only (breadth is allowed).
    if not q:
        return False, "empty question"
    if len(q) < min_len:
        return False, f"question too short (min {min_len} chars)"
    if len(q) > max_len:
        return False, f"question too long (max {max_len} chars)"
    if max_ctx > 0 and len(context or "") > max_ctx:
        return False, f"context too large (max {max_ctx} chars)"

    # Layer 2 — prohibited-use denylist (bundled: offensive-security + biology dual-use).
    allowed, label = check_denylist(question)
    if not allowed:
        if trusted:
            # Operator-authorized: block lifts, label is returned for audit logging.
            return True, label or "prohibited content (trusted)"
        return False, label or "prohibited content"

    return True, ""
