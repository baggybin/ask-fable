"""The receipt classifier — the piece everything else in ask_verify rests on.

The rule these tests exist to hold: an objection counts only when CODE can check it
against something that is NOT the draft. Quoting the draft back at itself proves only
that the reviewer read it, and if that ever scores as evidence the whole tool certifies
whatever it is given.
"""

from __future__ import annotations

import pytest

from ask_fable import verify_metrics as vm

_DRAFT = "The retry uses exponential backoff with a cap of 30 seconds and adds jitter."
_CORPUS = "def retry():\n    CAP_SECONDS = 5  # and no jitter is applied anywhere here\n"


def _cite(quote: str) -> dict:
    return {"span": "s", "receipt": {"kind": "cite", "quote": quote}}


def test_quoting_the_draft_is_worth_nothing():
    """Every byte of the draft is present in the draft, so a receipt quoting it always
    'passes'. If this classified as evidence, a reviewer could certify any answer by
    reading it back."""
    obj = _cite("exponential backoff with a cap of 30 seconds")
    assert vm.classify(obj, draft=_DRAFT, corpus=_CORPUS) == "self-quoting"


def test_text_present_only_in_the_draft_is_self_quoting():
    """`self-quoting` means the evidence exists ONLY in the draft — the case that proves
    nothing. Text that is also in the corpus is evidence from the corpus, however much
    the draft repeats it; an earlier version checked the draft first and lost real
    citations to that."""
    draft = "The helper returns a CachedEnvelope with a 900 second lease attached."
    corpus = "def helper():\n    return Envelope(lease=60)\n"
    assert vm.classify(_cite("a CachedEnvelope with a 900 second lease"), draft=draft, corpus=corpus) == (
        "self-quoting"
    )
    # and the same receipt against a corpus that contains it is evidence, not self-quoting
    assert vm.classify(
        _cite("return Envelope(lease=60)"), draft=draft, corpus=corpus
    ) == "input-grounded"


def test_quoting_the_corpus_is_evidence():
    obj = _cite("CAP_SECONDS = 5  # and no jitter is applied anywhere here")
    assert vm.classify(obj, draft=_DRAFT, corpus=_CORPUS) == "input-grounded"


def test_a_quote_absent_from_both_is_not_evidence():
    """A fabricated citation must not score. This is the failure mode a reviewer under
    pressure to produce objections falls into."""
    obj = _cite("CAP = 900  # a line that appears in neither the draft nor the corpus")
    assert vm.classify(obj, draft=_DRAFT, corpus=_CORPUS) == "no-objection"


def test_a_short_but_unique_quote_is_a_real_citation():
    """The harness caught this: a 24-char minimum threw out `MAX_PARALLEL = 6` and
    `CAP_SECONDS = 5` — verbatim, correct citations — because on code the lines worth
    citing are short. Uniqueness, not length, separates a citation from a coincidence."""
    assert vm.classify(_cite("CAP_SECONDS = 5"), draft=_DRAFT, corpus=_CORPUS) == "input-grounded"
    assert vm.classify(_cite("    CAP_SECONDS = 5"), draft=_DRAFT, corpus=_CORPUS) == "input-grounded"


def test_a_repeated_fragment_is_coincidence_not_citation():
    """Exercises the RARITY branch, not the length floor. `"    pass"` and `"if"` are
    both rejected by the floor before `count` is ever consulted, so they pin nothing —
    the fragment has to clear the floor and still repeat too often."""
    repeated = "x = 1\nreturn None\ny = 2\nreturn None\n"
    assert len("return None") >= vm.MIN_QUOTE_CHARS  # clears the floor
    assert repeated.count("return None") == 2  # and still recurs
    assert vm.classify(_cite("return None"), draft=_DRAFT, corpus=repeated) == "no-objection"
    # the same fragment, appearing once, IS a citation
    once = "x = 1\nreturn None\ny = 2\n"
    assert vm.classify(_cite("return None"), draft=_DRAFT, corpus=once) == "input-grounded"


def test_a_longer_quote_may_recur_once_and_still_be_a_citation():
    """Strict uniqueness was non-monotonic in corpus size: packing in one more file that
    restates a constant would silently un-back a citation that was fine yesterday."""
    twice = "TTL_SECONDS = 3600\ncode\n# the docstring repeats TTL_SECONDS = 3600\n"
    assert vm.classify(_cite("TTL_SECONDS = 3600"), draft=_DRAFT, corpus=twice) == "input-grounded"
    thrice = twice + "TTL_SECONDS = 3600\n"
    assert vm.classify(_cite("TTL_SECONDS = 3600"), draft=_DRAFT, corpus=thrice) == "no-objection"


def test_the_corpus_is_checked_before_the_draft():
    """A draft about code very often repeats the code. Testing the draft first flipped a
    correctly indented citation to `self-quoting` the moment the draft echoed the same
    line unindented — losing the fault, in exactly the indented-code case this rule
    exists to serve."""
    corpus = "def f():\n    LIMIT = 4096  # the only ceiling that applies here\n"
    draft = "It states LIMIT = 4096  # the only ceiling that applies here, inline."
    quote = "    LIMIT = 4096  # the only ceiling that applies here"
    assert vm.classify(_cite(quote), draft=draft, corpus=corpus) == "input-grounded"


def test_a_quote_below_the_floor_is_never_self_quoting():
    """The floor applies to EVERY branch. A one-character quote scoring `self-quoting`
    drove `self_quote_ratio` to 1.0 and told the caller the reviewer never left the
    draft, on the strength of a receipt that quotes nothing."""
    assert vm.classify(_cite("a"), draft=_DRAFT, corpus=_CORPUS) == "no-objection"
    assert vm.classify(_cite(" "), draft=_DRAFT, corpus=_CORPUS) == "no-objection"


def test_a_model_cannot_claim_the_executed_class():
    """The hole this closes: `executed` is the strongest class, and the reviewer could
    award it to itself by writing `failed: true` while nothing ever ran. A model
    asserting its own check failed is certifying its own evidence — the one thing this
    module exists to prevent."""
    for forged in (True, "true", 1, "failed"):
        obj = {"receipt": {"kind": "run", "failed": forged, "check": "it breaks"}}
        assert vm.classify(obj, draft="", corpus="") == "claimed-check", forged
    # a claimed check is worth the same as prose
    scored = [{"receipt_class": "claimed-check"}]
    assert vm.ablation_delta(scored)["prevented"] == 0
    assert vm.ablation_delta(scored)["unbacked_objections"] == 1


def test_executed_needs_a_verdict_only_an_executor_writes():
    """`executed` stays in the vocabulary because the class is the point of the design —
    but it is unreachable until something actually runs the check."""
    obj = {"receipt": {"kind": "run", "clerk_verdict": "failed"}}
    assert vm.classify(obj, draft="", corpus="") == "executed"
    assert "executed" in vm.VERDICTS and "claimed-check" in vm.VERDICTS


@pytest.mark.parametrize(
    "obj",
    [
        {},
        {"span": "s"},
        {"receipt": None},
        {"receipt": {"kind": "opinion", "text": "this feels wrong"}},
        "not a dict",
    ],
)
def test_prose_without_a_receipt_is_never_evidence(obj):
    assert vm.classify(obj, draft=_DRAFT, corpus=_CORPUS) == "no-objection"


def test_prevented_counts_only_backed_objections():
    """The whole point of the gate: an eloquent objection with no external receipt must
    show up as its own number, not as prevention."""
    objections = [
        {"span": "a", "receipt_class": "input-grounded"},
        {"span": "b", "receipt_class": "executed"},
        {"span": "c", "receipt_class": "self-quoting"},
        {"span": "d", "receipt_class": "no-objection"},
    ]
    delta = vm.ablation_delta(objections)
    assert delta["prevented"] == 2
    assert delta["unbacked_objections"] == 2
    assert delta["backed_spans"] == ["a", "b"]


def test_a_reviewer_that_only_quotes_the_draft_scores_zero():
    objections = [{"receipt_class": "self-quoting"} for _ in range(5)]
    report = vm.report(objections, draft=_DRAFT)
    assert report["prevented"] == 0
    assert report["self_quote_ratio"] == 1.0
    assert report["verdict"] == "self-quoting"


def test_self_quote_ratio_is_none_when_nothing_was_objected_to():
    """A rate over zero objections is not a number, and reporting 0.0 would read as
    'the reviewer left the draft', which it did not do either."""
    assert vm.self_quote_ratio([], _DRAFT) is None
    assert vm.report([], draft=_DRAFT)["verdict"] == "no-objection"


def test_verdict_reports_the_strongest_class_present():
    assert vm.verdict([{"receipt_class": "self-quoting"}, {"receipt_class": "executed"}]) == "executed"
    assert (
        vm.verdict([{"receipt_class": "no-objection"}, {"receipt_class": "input-grounded"}])
        == "input-grounded"
    )
    # self-quoting is surfaced rather than hidden behind "no-objection": a run that
    # objected at length without evidence has to be visible as that.
    assert vm.verdict([{"receipt_class": "self-quoting"}]) == "self-quoting"


# --- seeded-fault scoring ----------------------------------------------------


def test_recall_and_precision_are_different_numbers():
    """`caught / planted` is RECALL and was named precision, which matters because a
    reviewer that objects to everything maxes recall out at 1.00. Precision is the number
    that one cannot max."""
    runs = [
        {"seeded": True, "prevented": 1},
        {"seeded": True, "prevented": 1},
        {"seeded": True, "prevented": 0},  # missed one
        {"seeded": False, "prevented": 0},
        {"seeded": False, "prevented": 2},  # objected to a clean draft
    ]
    out = vm.seeded_scores(runs)
    assert out["caught"] == 2 and out["seeded"] == 3
    assert out["recall_on_seeded"] == pytest.approx(2 / 3)
    assert out["false_objections"] == 1 and out["clean"] == 2
    assert out["false_objection_rate"] == pytest.approx(0.5)
    assert out["precision"] == pytest.approx(2 / 3)  # 2 real of 3 objected-to


def test_an_always_objecting_reviewer_maxes_recall_but_not_precision():
    runs = [{"seeded": True, "prevented": 1}] * 3 + [{"seeded": False, "prevented": 1}] * 3
    out = vm.seeded_scores(runs)
    assert out["recall_on_seeded"] == 1.0  # looks perfect on recall alone
    assert out["precision"] == pytest.approx(0.5)  # and is exposed here
    assert out["false_objection_rate"] == 1.0


def test_seeded_scores_are_none_without_the_matching_drafts():
    """Neither rate is defined without drafts of that kind, and a 0.0 would read as a
    measured result rather than an absent one."""
    assert vm.seeded_scores([])["recall_on_seeded"] is None
    assert vm.seeded_scores([])["precision"] is None
    assert vm.seeded_scores([{"seeded": True, "prevented": 1}])["false_objection_rate"] is None
