"""``ask_verify`` — the handler, block extraction, and the one invariant v1 must never
break: the draft comes back, whatever the reviewer says.

Guard and backends stubbed; no model is called.
"""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.server as server
from ask_fable import oracles, verify
from ask_fable.oracle_common import OracleResult

_DRAFT = "The retry uses exponential backoff with a cap of 30 seconds and adds jitter."
_CORPUS = "def retry():\n    CAP = 5  # seconds, and no jitter is applied anywhere here\n"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.guard, "check", lambda q, c="", **kw: (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.outputs, "save", lambda **k: "/tmp/fake-transcript")
    monkeypatch.setattr(server, "_hub_mirror", lambda **k: None)


def _run(coro):
    return asyncio.run(coro)


def _block(objections: str) -> str:
    return f"Some prose about the draft.\n\n```json-verify\n{{\"verify_version\": 1, \"objections\": [{objections}]}}\n```"


_GOOD_CITE = (
    '{"span": "cap of 30 seconds", "claim": "the cap is 5s",'
    ' "receipt": {"kind": "cite", "quote": "CAP = 5  # seconds, and no jitter is applied anywhere here"}}'
)
_SELF_CITE = (
    '{"span": "backoff", "claim": "restating the draft",'
    ' "receipt": {"kind": "cite", "quote": "exponential backoff with a cap of 30 seconds"}}'
)
_PROSE_ONLY = '{"span": "jitter", "claim": "feels wrong", "receipt": {"kind": "opinion"}}'


def _stub(monkeypatch, text: str = "", *, status: str = "ok", kind: str = ""):
    seen: dict = {}

    async def fake_run(key, question, context="", **kw):
        seen["prompt"] = question
        if status != "ok":
            return OracleResult(status, key=key, kind=kind, text="reviewer down", model=key)
        return OracleResult("ok", key=key, text=text, model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    return seen


def _args(**over) -> dict:
    base = {"question": "Is the retry capped?", "answer": _DRAFT, "context": _CORPUS}
    base.update(over)
    return base


# --- the invariant -----------------------------------------------------------


def test_the_draft_is_returned_unchanged(monkeypatch):
    _stub(monkeypatch, _block(_GOOD_CITE))
    out = _run(verify._handle_verify(_args()))
    assert out["status"] == "ok" and out["answer"] == _DRAFT


def test_a_failed_review_still_returns_the_draft(monkeypatch):
    """The reviewer failing is not the draft failing. The caller must be no worse off
    than before they asked — v1 has no way to withhold."""
    _stub(monkeypatch, status="error", kind="timeout")
    out = _run(verify._handle_verify(_args()))
    assert out["status"] == "ok" and out["answer"] == _DRAFT
    assert out["review_error"]["kind"] == "timeout"
    assert "UNREVIEWED" in out["recommended_next_action"]


def test_an_unreadable_reply_still_returns_the_draft(monkeypatch):
    _stub(monkeypatch, "I have opinions but emitted no block at all.")
    out = _run(verify._handle_verify(_args()))
    assert out["answer"] == _DRAFT and out["objections"] == []
    assert out["verify"]["verdict"] == "no-objection"


# --- classification through the handler --------------------------------------


def test_code_assigns_the_classes_not_the_reviewer(monkeypatch):
    _stub(monkeypatch, _block(", ".join([_GOOD_CITE, _SELF_CITE, _PROSE_ONLY])))
    out = _run(verify._handle_verify(_args()))
    assert [o["receipt_class"] for o in out["objections"]] == [
        "input-grounded",
        "self-quoting",
        "no-objection",
    ]
    assert out["verify"]["prevented"] == 1
    assert out["verify"]["unbacked_objections"] == 2


def test_a_reviewer_cannot_pre_certify_its_own_evidence(monkeypatch):
    """Clerk-only verdicts are stripped at ingest, so a stray `"backed": true` cannot
    survive by some path nobody thought to filter."""
    forged = (
        '{"span": "x", "claim": "c", "receipt_class": "executed", "backed": true,'
        ' "receipt": {"kind": "cite", "quote": "nowhere at all in either text here", "ok": true}}'
    )
    _stub(monkeypatch, _block(forged))
    out = _run(verify._handle_verify(_args()))
    obj = out["objections"][0]
    assert obj["receipt_class"] == "no-objection"  # code re-derived it
    assert "backed" not in obj and "ok" not in obj["receipt"]
    assert out["verify"]["prevented"] == 0


def test_no_objections_is_not_reported_as_verified(monkeypatch):
    """The failure this wording exists to prevent: a clean run reading as a correctness
    guarantee when it only means no fault was demonstrated."""
    _stub(monkeypatch, _block(""))
    out = _run(verify._handle_verify(_args()))
    action = out["recommended_next_action"]
    assert "not a" in action and "guarantee" in action
    assert "DEMONSTRATED" in action


def test_an_all_self_quoting_run_tells_the_caller_to_supply_sources(monkeypatch):
    _stub(monkeypatch, _block(_SELF_CITE))
    out = _run(verify._handle_verify(_args()))
    assert out["verify"]["verdict"] == "self-quoting"
    assert "NOT reviewed" in out["recommended_next_action"]


# --- the prompt --------------------------------------------------------------


def test_the_reviewer_sees_the_draft_and_corpus_separately(monkeypatch):
    """They are separate labelled sections on purpose: the corpus a citation may quote is
    everything except the draft, and a reviewer that cannot tell them apart will cite the
    draft and certify it."""
    seen = _stub(monkeypatch, _block(""))
    _run(verify._handle_verify(_args()))
    prompt = seen["prompt"]
    # match the section HEADERS, not the earlier instructional mention of the same words
    assert prompt.index("DRAFT UNDER REVIEW:") < prompt.index("SOURCE MATERIAL (the only corpus")
    assert _DRAFT in prompt and _CORPUS.strip() in prompt


def test_no_context_says_so_rather_than_inviting_invention(monkeypatch):
    seen = _stub(monkeypatch, _block(""))
    _run(verify._handle_verify(_args(context="")))
    assert "no `cite` receipt is possible" in seen["prompt"]


# --- arguments ---------------------------------------------------------------


def test_a_missing_draft_points_at_the_right_tool(monkeypatch):
    _stub(monkeypatch)
    out = _run(verify._handle_verify({"question": "q"}))
    assert out["kind"] == "bad_args"
    assert "ask_debate or ask_falsify" in out["detail"]


def test_a_missing_question_is_bad_args(monkeypatch):
    _stub(monkeypatch)
    assert _run(verify._handle_verify({"answer": _DRAFT}))["kind"] == "bad_args"


def test_a_same_lab_review_is_refused(monkeypatch):
    """A family grading its own homework agrees with itself for reasons unrelated to the
    draft being right."""
    _stub(monkeypatch, _block(""))
    out = _run(verify._handle_verify(_args(reviewer="opus", drafted_by="fable")))
    assert out["kind"] == "bad_args" and "different labs" in out["detail"]


def test_a_cross_lab_review_is_allowed(monkeypatch):
    _stub(monkeypatch, _block(""))
    out = _run(verify._handle_verify(_args(reviewer="minimax", drafted_by="fable")))
    assert out["status"] == "ok"


def test_an_unknown_reviewer_is_rejected_before_any_call(monkeypatch):
    called = []

    async def boom(key, question, context="", **kw):
        called.append(key)
        return OracleResult("ok", key=key, text="", model=key)

    monkeypatch.setattr(oracles, "run", boom)
    out = _run(verify._handle_verify(_args(reviewer="gpt-4")))
    assert out["kind"] == "no_models" and called == []


# --- block extraction --------------------------------------------------------


def test_two_blocks_are_stripped_but_not_trusted():
    """Guessing which block counts is how a wrong objection gets authority."""
    text = _block(_GOOD_CITE) + "\n" + _block(_SELF_CITE)
    prose, objections, _read = verify.extract(text)
    assert objections == []
    assert "verify_version" not in prose  # neither leaks into the prose


def test_an_unfenced_block_with_the_sentinel_is_still_read():
    text = 'prose\n\n```json\n{"verify_version": 1, "objections": [{"span": "a"}]}\n```'
    _prose, objections, _read = verify.extract(text)
    assert [o["span"] for o in objections] == ["a"]


def test_objections_are_capped():
    many = ", ".join(f'{{"span": "s{i}"}}' for i in range(40))
    _prose, objections, _read = verify.extract(_block(many))
    assert len(objections) == verify._MAX_OBJECTIONS


def test_a_non_list_objections_field_is_ignored():
    _prose, objections, _read = verify.extract(
        '```json-verify\n{"verify_version": 1, "objections": "lots"}\n```'
    )
    assert objections == []


# --- registration ------------------------------------------------------------


def test_the_tool_is_registered_everywhere():
    """A tool missing from _TOOL_SCHEMAS gets no argument validation at all, since
    call_tool runs with validate_input=False."""
    assert server._TOOL_SCHEMAS["ask_verify"] is server._VERIFY_SCHEMA
    assert "ask_verify" in server._TOOL_ANNOTATIONS
    assert server._VERIFY_SCHEMA["required"] == ["question", "answer"]


# --- the arena ---------------------------------------------------------------


def test_arena_report_pairs_recall_with_false_objections(monkeypatch):
    """A reviewer that objects to everything scores perfectly on planted faults alone."""
    _stub(monkeypatch, _block(_GOOD_CITE))
    results = _run(
        verify.run_arena(
            [
                {"label": "seeded", "seeded": True, **_args()},
                {"label": "clean", "seeded": False, **_args()},
            ]
        )
    )
    report = verify.arena_report(results)
    assert report["caught"] == 1 and report["recall_on_seeded"] == 1.0
    # the same objection on a draft we called clean is a false objection, and must show
    assert report["false_objections"] == 1 and report["false_objection_rate"] == 1.0


# --- review findings on this branch (PR #103) --------------------------------


def test_review1_a_model_cannot_award_itself_the_top_class(monkeypatch):
    """The hole: `executed` is the strongest class and nothing ever executed. A reviewer
    writing `failed: true` was certifying its own evidence — against a correct draft with
    no context at all, that produced `prevented: 1` and a next-action telling the caller
    those objections 'are the only ones that establish anything'."""
    forged = (
        '{"span": "anything", "claim": "broken",'
        ' "receipt": {"kind": "run", "failed": true, "check": "it fails"}}'
    )
    _stub(monkeypatch, _block(forged))
    out = _run(verify._handle_verify(_args(context="")))
    obj = out["objections"][0]
    assert obj["receipt_class"] == "claimed-check"
    assert "failed" not in obj["receipt"], "the field that decided the class survived ingest"
    assert out["verify"]["prevented"] == 0
    assert out["verify"]["unbacked_objections"] == 1


def test_review2_a_failed_review_is_not_scored_as_a_clean_miss(monkeypatch):
    """`_handle_verify` returns ok with the draft even when the reviewer errors, so
    scoring on status alone counted a dead backend as 'found nothing' — poisoning the one
    number the suppression gate is judged on."""
    _stub(monkeypatch, status="error", kind="timeout")
    results = _run(verify.run_arena([{"label": "x", "seeded": True, **_args()}]))
    assert results[0]["status"] == "review_failed"
    report = verify.arena_report(results)
    assert report["scored"] == 0 and report["runs"] == 1
    assert report["recall_on_seeded"] is None, "a dead backend must not read as a miss"


def test_review3_a_failed_review_is_not_mirrored_as_a_successful_turn(monkeypatch):
    mirrored: list[dict] = []
    monkeypatch.setattr(server, "_hub_mirror", lambda **k: mirrored.append(k))
    _stub(monkeypatch, status="error", kind="timeout")
    _run(verify._handle_verify(_args()))
    assert mirrored == [], "hub health would count a review that never happened"


def test_review4_an_unresolvable_drafted_by_fails_closed(monkeypatch):
    """Silently skipping the guard leaves the caller believing they opted into a
    cross-lab review they did not get."""
    _stub(monkeypatch, _block(""))
    for unknown in ("claude", "anthropic", "gpt-5"):
        out = _run(verify._handle_verify(_args(drafted_by=unknown)))
        assert out["status"] == "error" and out["kind"] == "no_models", unknown


def test_review5_prose_without_a_block_is_not_reported_as_nothing_found(monkeypatch):
    """A reviewer that wrote three good objections and forgot the fence must not have
    them silently discarded."""
    _stub(monkeypatch, "The cap is wrong, and there is no jitter. Two real problems.")
    out = _run(verify._handle_verify(_args()))
    assert out["block_unreadable"] is True
    assert "no jitter" in out["reviewer_prose"]
    assert "NOT 'it found nothing'" in out["recommended_next_action"]


def test_review5_an_empty_block_is_a_real_clean_bill_of_health(monkeypatch):
    """The contrast: a readable block that happens to be empty is not 'unreadable'."""
    _stub(monkeypatch, _block(""))
    out = _run(verify._handle_verify(_args()))
    assert "block_unreadable" not in out
    assert "DEMONSTRATED" in out["recommended_next_action"]


def test_review5_two_blocks_are_flagged_rather_than_read_as_empty(monkeypatch):
    _stub(monkeypatch, _block(_GOOD_CITE) + "\n" + _block(_SELF_CITE))
    out = _run(verify._handle_verify(_args()))
    assert out["block_unreadable"] is True


# --- the run-receipt executor ------------------------------------------------


def _run_receipt(code: str = "raise SystemExit(1)") -> str:
    import json as _json

    # json.dumps, not repr: repr emits single quotes and the block would not parse.
    return (
        '{"span": "the cap", "claim": "the check fails",'
        f' "receipt": {{"kind": "run", "code": {_json.dumps(code)}}}}}'
    )


def _sandbox(monkeypatch, *, enabled=True, available=True, self_test=True, status="fail",
             stderr=""):
    from ask_fable import sandbox as sb

    monkeypatch.setattr(sb, "enabled", lambda: enabled)
    monkeypatch.setattr(sb, "available", lambda: available)

    async def fake_self_test():
        return self_test

    async def fake_run(code):
        return sb.SandboxResult(status, None, "", stderr)

    monkeypatch.setattr(sb, "self_test", fake_self_test)
    monkeypatch.setattr(sb, "run_python", fake_run)


def test_a_check_that_runs_and_fails_is_the_strongest_class(monkeypatch):
    _allow_verify = _stub(monkeypatch, _block(_run_receipt()))
    _sandbox(monkeypatch, status="fail")
    out = _run(verify._handle_verify(_args()))
    obj = out["objections"][0]
    assert obj["receipt_class"] == "executed"
    assert obj["receipt"]["clerk_verdict"] == "failed"
    assert out["verify"]["prevented"] == 1
    assert "CODE EXECUTION IS ENABLED" in _allow_verify["prompt"]


def test_a_check_that_passes_is_evidence_for_the_draft_not_against(monkeypatch):
    """Counting it as an objection would let a reviewer inflate its numbers by submitting
    checks it expects to pass — but it is not 'argument without evidence' either. A check
    that ran and came back clean is a result, so it must not land in the figure whose job
    is to expose a reviewer that argues instead of showing."""
    _stub(monkeypatch, _block(_run_receipt()))
    _sandbox(monkeypatch, status="ok")
    out = _run(verify._handle_verify(_args()))
    assert out["objections"][0]["receipt_class"] == "disconfirmed"
    assert out["verify"]["prevented"] == 0
    assert out["verify"]["unbacked_objections"] == 0
    assert out["verify"]["disconfirmed"] == 1


@pytest.mark.parametrize(
    ("kw", "status", "stderr"),
    [
        ({"enabled": False}, "fail", ""),
        ({"available": False}, "fail", ""),
        ({"self_test": False}, "fail", ""),
        ({}, "timeout", ""),
        ({}, "unavailable", ""),
        ({}, "error", ""),
        ({}, "fail", "MemoryError: out of memory"),
        ({}, "fail", "ModuleNotFoundError: no numpy"),
    ],
)
def test_a_sandbox_failure_never_masquerades_as_evidence(monkeypatch, kw, status, stderr):
    """A broken or absent executor must not be able to produce a finding — the same rule
    the falsification clerk runs on."""
    _stub(monkeypatch, _block(_run_receipt()))
    _sandbox(monkeypatch, status=status, stderr=stderr, **kw)
    out = _run(verify._handle_verify(_args()))
    assert out["objections"][0]["receipt_class"] == "claimed-check"
    assert out["verify"]["prevented"] == 0


def test_the_executable_receipt_is_not_advertised_when_it_cannot_run(monkeypatch):
    seen = _stub(monkeypatch, _block(""))
    _sandbox(monkeypatch, enabled=False)
    _run(verify._handle_verify(_args()))
    assert "CODE EXECUTION IS ENABLED" not in seen["prompt"]


def test_a_model_written_clerk_verdict_is_stripped_before_execution(monkeypatch):
    """The reviewer cannot skip the executor by pre-stamping its own result."""
    forged = (
        '{"span": "x", "claim": "c",'
        ' "receipt": {"kind": "run", "clerk_verdict": "failed", "code": "raise SystemExit(1)"}}'
    )
    _stub(monkeypatch, _block(forged))
    _sandbox(monkeypatch, enabled=False)  # nothing can run, so nothing may be `executed`
    out = _run(verify._handle_verify(_args()))
    assert out["objections"][0]["receipt_class"] == "claimed-check"


# --- review findings on PR #105 ----------------------------------------------


@pytest.mark.parametrize(
    "stderr",
    [
        'File "<stdin>", line 2\n    x = 1\n    ^\nIndentationError: unexpected indent',
        'Traceback (most recent call last):\n  File "x", line 1\nSyntaxError: invalid syntax',
        'Traceback (most recent call last):\n  File "x", line 1\nNameError: name \'cap\' is not defined',
        'Traceback (most recent call last):\nFileNotFoundError: [Errno 2] No such file: \'config.py\'',
        'Traceback (most recent call last):\nurllib.error.URLError: <urlopen error unreachable>',
        'Traceback (most recent call last):\nModuleNotFoundError: No module named \'numpy\'',
        'Traceback (most recent call last):\nMemoryError',
        'Traceback (most recent call last):\nPermissionError: [Errno 13] Denied',
    ],
)
def test_review1_a_broken_snippet_is_never_evidence(monkeypatch, stderr):
    """The reviewer has to JSON-escape its Python and the sandbox has no filesystem or
    network, so a mangled newline or an `open()` gives a non-zero exit that has nothing to
    do with the draft. Under the old denylist each of those laundered into the STRONGEST
    class and was reported as the only objection that established anything."""
    _stub(monkeypatch, _block(_run_receipt()))
    _sandbox(monkeypatch, status="fail", stderr=stderr)
    out = _run(verify._handle_verify(_args()))
    assert out["objections"][0]["receipt_class"] == "claimed-check", stderr
    assert out["verify"]["prevented"] == 0


@pytest.mark.parametrize(
    "stderr",
    [
        "",  # `raise SystemExit(1)` prints nothing
        'Traceback (most recent call last):\n  File "x", line 3, in <module>\nAssertionError: cap was 30, expected 5',
    ],
)
def test_review1_a_genuine_check_failure_still_counts(monkeypatch, stderr):
    _stub(monkeypatch, _block(_run_receipt()))
    _sandbox(monkeypatch, status="fail", stderr=stderr)
    out = _run(verify._handle_verify(_args()))
    assert out["objections"][0]["receipt_class"] == "executed", stderr
    assert out["verify"]["prevented"] == 1


def test_review1_an_unrecognised_exception_fails_closed(monkeypatch):
    """Unclassified fails closed: an exception nobody enumerated is a snippet that did not
    run, never a refutation."""
    _stub(monkeypatch, _block(_run_receipt()))
    _sandbox(monkeypatch, status="fail", stderr="Traceback:\nSomeVendorSpecificError: boom")
    out = _run(verify._handle_verify(_args()))
    assert out["objections"][0]["receipt_class"] == "claimed-check"


def test_review1_the_exception_is_read_from_the_last_line():
    """Python names the exception at the END of a traceback, and a chatty snippet puts
    plenty in front of it."""
    noisy = "AssertionError mentioned in passing\n" * 5 + "ZeroDivisionError: division by zero"
    assert verify._failure_kind(noisy) == "snippet"
    assert verify._failure_kind("noise\nmore noise\nAssertionError: real") == "check"


def test_review5_the_executor_has_a_total_budget(monkeypatch):
    """Twelve receipts at the per-run timeout is minutes inside one tool call."""
    import asyncio as _asyncio

    from ask_fable import sandbox as sb

    ran: list[str] = []

    async def slow(code):
        ran.append(code)
        await _asyncio.sleep(0.02)
        return sb.SandboxResult("fail", 1, "", "")

    monkeypatch.setattr(sb, "run_python", slow)
    objs = [{"receipt": {"kind": "run", "code": f"check{i}"}} for i in range(8)]
    _run(verify._execute(objs, True, budget_s=0.03))
    assert len(ran) < 8, "no budget was applied"
    skipped = [o for o in objs if o["receipt"]["clerk_verdict"] == "inconclusive"]
    assert skipped, "unrun receipts must be inconclusive, never a finding"


def test_review7_a_padded_kind_is_treated_the_same_by_both_predicates(monkeypatch):
    """The predicate deciding whether code RUNS and the one deciding what it MEANS must
    be the same expression, or a receipt runs and is classified as something else."""
    from ask_fable import verify_metrics as vm

    obj = {"receipt": {"kind": " RUN ", "code": "raise SystemExit(1)"}}
    _sandbox(monkeypatch, status="fail")
    _run(verify._execute([obj], True))
    assert obj["receipt"]["clerk_verdict"] == "failed"
    assert vm.classify(obj, draft="d", corpus="c") == "executed"
