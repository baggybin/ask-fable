"""Receipt classification and acceptance metrics for ``ask_verify`` — is a reviewer's
objection *evidence*, or is it prose?

The conference that asked for a red-team veto left one unresolved contradiction: the
veto must block (or the reviewer is decoration nobody reads) versus the veto must not
block (because "when the 'defender' is also a model, is a mirror — mirrors generate
confident nonsense"). The resolution this module implements moves the veto off the
ANSWER and onto the CLAIM, and makes the thing that could ever suppress be CODE rather
than the reviewer — the same discipline ``falsify.py`` already runs, where the clerk is
code and "a model can't certify its own evidence".

So an objection is classified by what its receipt can be checked against, never by what
the reviewer asserts about it:

- ``no-objection``   — nothing was challenged. Nothing was tested.
- ``self-quoting``   — the only evidence is the draft's own text. Worth NOTHING: every
  byte of the draft is trivially present in the draft, so a receipt quoting it always
  "passes". A reviewer scoring here is reading the answer back to itself.
- ``input-grounded`` — quotes the caller's supplied corpus (NOT the draft), contradicting
  the draft. Mechanically checkable, so it can carry weight.
- ``disconfirmed``   — a check ran and PASSED, so the draft survived it. Not an objection,
  and deliberately not counted as an unbacked one either: it is a real result.
- ``claimed-check``  — the reviewer SAYS a check fails, but nothing ran it. Worth the
  same as prose: a model asserting its own check failed is certifying its own evidence,
  which is the one thing this module exists to prevent.
- ``executed``       — a check actually ran, under the sandbox, and FAILED. The strongest
  class, because the evidence was manufactured rather than looked up. Reachable only when
  the operator has turned code execution on (``ASK_FABLE_ALLOW_RUN`` + ``bwrap``); a check
  that ran and passed is evidence FOR the draft and scores nothing, and a sandbox that was
  unavailable or fell over is inconclusive, never a finding.

Only the last two may ever count as prevention. That gate is the whole point: without it
an eloquent, confident, unfalsifiable objection scores exactly like a true one, and the
metric rewards the failure mode the feature exists to catch.

Pure functions over dicts. No I/O, no model calls — the real-model runner lives in
``scripts/verify_arena.py``, mirroring ``falsify_metrics`` / ``scripts/falsify_arena.py``.
"""

from __future__ import annotations

from typing import Final

from .falsify_ledger import cite_supported

# Classes whose evidence is checkable against something that is not the draft. Only
# these may count toward `prevented`.
BACKED_CLASSES: Final = ("input-grounded", "executed")

# Verdict vocabulary, deliberately parallel to falsify_metrics.report's
# no-falsification / retrieval-bound / frontier-probing ladder.
VERDICTS: Final = (
    "no-objection",
    "disconfirmed",
    "self-quoting",
    "claimed-check",
    "input-grounded",
    "executed",
)

# What separates a citation from a coincidence is UNIQUENESS, not length. A 24-char
# minimum looked safe and was wrong: on code the lines worth citing are short —
# `MAX_PARALLEL = 6`, `CAP_SECONDS = 5`, `KEY_VERSION = 5` — and the harness caught the
# reviewer quoting exactly those, verbatim and correctly, only to have them thrown out as
# "too short". A quote that appears exactly once in the corpus is a citation at any
# length; a short one that appears repeatedly ("if x:", "return") is not evidence of
# anything.
MIN_QUOTE_CHARS: Final = 6  # floor: below this even a unique hit is noise
RARE_QUOTE_CHARS: Final = 12  # at or above this, appearing twice is still distinctive
DISTINCTIVE_QUOTE_CHARS: Final = 24  # at or above this, length alone carries it


def _is_citable(quote: str, corpus: str) -> bool:
    """Whether ``quote`` is specific enough to be a citation rather than a coincidence.

    Rarity, scaled by length. Strict uniqueness (``count == 1``) turned out to be
    non-monotonic in corpus size: a citation backed today silently stops being backed
    tomorrow because the caller packed in one more file that restates the line. A
    constant repeated in a module and its test — ``TTL_SECONDS = 3600`` twice in a 10 kB
    pack — is still a perfectly distinctive citation. So a longer quote is allowed to
    recur once, while a very short one must be unique or it is `return`-shaped noise.

    (``str.count`` counts non-overlapping occurrences, so a self-overlapping fragment can
    under-count; the length floor is what keeps that harmless.)
    """
    stripped = quote.strip()
    if len(stripped) < MIN_QUOTE_CHARS:
        return False
    if len(stripped) >= DISTINCTIVE_QUOTE_CHARS:
        return True
    occurrences = (corpus or "").count(stripped)
    limit = 2 if len(stripped) >= RARE_QUOTE_CHARS else 1
    return 1 <= occurrences <= limit


def classify(objection: dict, *, draft: str, corpus: str) -> str:
    """The receipt class of one objection. Code decides this; the reviewer does not.

    Order matters. ``self-quoting`` is checked BEFORE ``input-grounded`` because a draft
    that repeats its inputs would otherwise let a reviewer quote the draft and have it
    score as corpus-grounded — self-certification through the back door.
    """
    if not isinstance(objection, dict):
        return "no-objection"
    receipt = objection.get("receipt")
    if not isinstance(receipt, dict):
        return "no-objection"
    kind = str(receipt.get("kind") or "").strip().lower()

    if kind == "run":
        # `executed` is assignable ONLY from an execution result code produced itself —
        # a `verdict` field the clerk writes after actually running the snippet, which v1
        # has no executor for. A model writing `"failed": true` is asserting that its own
        # check failed, i.e. certifying its own evidence, so it lands in `claimed-check`
        # and counts for nothing. (`falsify._run_verdict` maps a disabled or broken
        # sandbox to inconclusive for the same reason: a failure to run must never
        # masquerade as a finding.)
        verdict = receipt.get("clerk_verdict")
        if verdict == "failed":
            return "executed"  # the check ran and the draft's claim did not hold
        if verdict == "passed":
            # It ran and the draft survived it. Not an objection — counting it would let a
            # reviewer inflate its numbers by submitting checks it expects to pass — but
            # not "argument without evidence" either: a check that executed and
            # disconfirmed the objection is a stronger result than prose, and folding it
            # into `unbacked_objections` slandered the reviewer with the very number whose
            # job is to expose one that argues instead of showing.
            return "disconfirmed"
        # "inconclusive", or no verdict at all: nothing ran, so this is the reviewer
        # asserting that its own check fails — a claim, not a result.
        return "claimed-check"

    if kind == "cite":
        quote = str(receipt.get("quote") or "")
        stripped = quote.strip()
        if len(stripped) < MIN_QUOTE_CHARS:
            # The floor comes first, for EVERY branch. Checking self-quoting ahead of it
            # let a one-character quote score `self-quoting`, which then drove
            # `self_quote_ratio` to 1.0 and told the caller "the reviewer never left the
            # draft" on the strength of a receipt that quotes nothing.
            return "no-objection"
        # The CORPUS is checked first. Text present in the source material is evidence
        # from the source, whether or not the draft happens to repeat it — and drafts
        # about code very often do. Testing the draft first flipped a correctly indented
        # code citation to `self-quoting` the moment the draft echoed the same line
        # unindented, losing the fault. `self-quoting` means the evidence exists ONLY in
        # the draft, which is the thing that proves nothing.
        if cite_supported(receipt, corpus or ""):
            return "input-grounded" if _is_citable(quote, corpus or "") else "no-objection"
        return "self-quoting" if stripped in (draft or "") else "no-objection"

    return "no-objection"


def self_quote_ratio(objections: list[dict], draft: str) -> float | None:
    """Fraction of objections whose only evidence was the draft itself.

    The ``grep_ratio`` analogue. 1.0 means the reviewer never once left the draft — it is
    self-certifying, and its objections establish nothing. ``None`` when nothing was
    objected to, because a rate over zero objections is not a number.
    """
    scored = [o for o in objections or [] if isinstance(o, dict)]
    if not scored:
        return None
    selfish = sum(1 for o in scored if o.get("receipt_class") == "self-quoting")
    return selfish / len(scored)


def ablation_delta(objections: list[dict]) -> dict:
    """What the reviewer PREVENTED, relative to shipping the draft unreviewed.

    The counterfactual here needs no shadow copy the way ``falsify_metrics`` needs one:
    with the reviewer off, the draft ships whole, so "what consensus would have accepted"
    is simply the draft. The number that matters is how much of the objection traffic was
    actually backed.

    ``prevented`` counts ONLY receipt-backed objections. ``unbacked_objections`` counts
    the rest as its own figure rather than folding them in, so a reviewer whose
    unbacked count dwarfs its prevented count is visible as the mirror it is instead of
    reading as diligence.
    """
    scored = [o for o in objections or [] if isinstance(o, dict)]
    backed = [o for o in scored if o.get("receipt_class") in BACKED_CLASSES]
    # `disconfirmed` is neither: the check ran and came back clean, which is a result, not
    # an unsupported assertion.
    disconfirmed = [o for o in scored if o.get("receipt_class") == "disconfirmed"]
    return {
        "prevented": len(backed),
        "unbacked_objections": len(scored) - len(backed) - len(disconfirmed),
        **({"disconfirmed": len(disconfirmed)} if disconfirmed else {}),
        "backed_spans": sorted({str(o.get("span") or "") for o in backed if o.get("span")}),
    }


def verdict(objections: list[dict]) -> str:
    """One word for the run, strongest class present.

    ``self-quoting`` is reported rather than hidden: a run whose every objection quotes
    the draft has tested nothing, and saying "no-objection" there would conceal that the
    reviewer spoke at length without evidence.
    """
    classes = {o.get("receipt_class") for o in objections or [] if isinstance(o, dict)}
    if "executed" in classes:
        return "executed"
    if "input-grounded" in classes:
        return "input-grounded"
    if "self-quoting" in classes:
        return "self-quoting"
    return "no-objection"


def report(objections: list[dict], *, draft: str) -> dict:
    """The acceptance report for one ``ask_verify`` run."""
    scored = [o for o in objections or [] if isinstance(o, dict)]
    return {
        "verdict": verdict(scored),
        "objections": len(scored),
        "self_quote_ratio": self_quote_ratio(scored, draft),
        **ablation_delta(scored),
        "note": (
            "`prevented` counts only objections whose receipt code could check against "
            "something other than the draft; an objection quoting the draft is "
            "self-certifying and counts for nothing. A high `unbacked_objections` next "
            "to a low `prevented` is a reviewer arguing rather than showing."
        ),
    }


def seeded_scores(runs: list[dict]) -> dict:
    """Recall, precision and false-objection rate over a SEEDED set — the only judge-free signal
    that says whether objections are *right* rather than merely present.

    Each run is ``{"seeded": bool, "prevented": int}``: ``seeded`` marks a draft with a
    known injected fault, and a run with no seeded fault is a known-clean draft that the
    reviewer must leave alone.

    The ablation above measures effect and attribution; it cannot measure direction,
    because a confident wrong objection counts exactly like a true one. Mutation-testing
    the reviewer is what closes that gap: score it against faults we planted ourselves,
    so no model has to grade another model's correctness.
    """
    faulty = [r for r in runs or [] if isinstance(r, dict) and r.get("seeded")]
    clean = [r for r in runs or [] if isinstance(r, dict) and not r.get("seeded")]
    caught = sum(1 for r in faulty if int(r.get("prevented") or 0) > 0)
    false_alarms = sum(1 for r in clean if int(r.get("prevented") or 0) > 0)
    return {
        "seeded": len(faulty),
        "caught": caught,
        # RECALL, and named as such. `caught / planted` answers "of the faults we know are
        # there, how many did it catch" — a reviewer that objects to every draft scores
        # 1.00 here, so this number alone is not a gate. Precision below is the other half.
        "recall_on_seeded": (caught / len(faulty)) if faulty else None,
        "clean": len(clean),
        "false_objections": false_alarms,
        # The cost side: how often it objects — with evidence — to a draft we know is fine.
        "false_objection_rate": (false_alarms / len(clean)) if clean else None,
        # True precision: of everything it objected to, how much was a real fault. This is
        # the number that a always-object reviewer cannot max out.
        "precision": (caught / (caught + false_alarms)) if (caught + false_alarms) else None,
    }
