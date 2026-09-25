"""``ask_verify`` — put an ALREADY-WRITTEN answer in front of a reviewer that has to
show its work.

Every other mode here reasons from scratch: ``ask_council`` fans a question out,
``ask_debate`` grows its own position, ``ask_falsify`` asserts its own claims. None of
them can be handed a draft someone else produced. This one can, and that turns out to be
the whole difficulty, because the obvious shortcut is broken: you cannot pass the draft
to ``ask_falsify`` as its ``context``, since ``context`` is the corpus receipts are
checked against, and every sentence of a draft is trivially present in that draft. The
answer would certify itself and report a clean bill of health.

So the corpus here is deliberately **everything except the draft**. An objection counts
only when code can check it against the caller's supplied inputs or against a check that
ran — see :mod:`verify_metrics` for the four receipt classes.

**v1 never suppresses.** The draft always ships, whatever the reviewer says, and the
objections ride alongside it with their classes attached. That is not timidity: a
reviewer that turns out to be decoration can be removed later, but an answer suppressed
in error is invisible to the caller, who cannot recover from what they never saw. The
metrics this emits are what earns the right to suppress — precision on seeded faults
(``scripts/verify_arena.py``) gates the revision round, not an argument about design.

The model is never asked for a verdict. It emits objections with receipts; the CODE in
:mod:`verify_metrics` assigns every class. A model cannot certify its own evidence, the
same rule the falsification clerk runs on.
"""

from __future__ import annotations

import re
import time

from . import audit, oracles, outputs, sandbox, trace_runtime, verify_metrics
from . import debate_ledger as dl
from .console import Reporter
from .prompts import compose_verify

FENCE = "json-verify"  # never collides with json-sidecar / json-debate / json-falsify
SENTINEL = "verify_version"  # required key; presence disambiguates an unfenced block

# Verdicts only code may write. A reviewer that pre-stamps its own objection as backed is
# certifying its own evidence, so these are stripped at ingest.
CLERK_FIELDS = (
    "receipt_class",
    "backed",
    "ok",
    "verified",
    "prevented",
    # `failed` and `clerk_verdict` decide the STRONGEST class, so a model writing either
    # would be certifying its own evidence — the one thing this design exists to stop.
    # Only an executor may write them, and v1 has none.
    "failed",
    "clerk_verdict",
)

_MAX_OBJECTIONS = 12  # a reviewer listing more than this is padding, not reviewing
_RUN_BUDGET_S = 30.0  # total wall clock for ALL run receipts in one call

# Exceptions that mean THE CHECK FAILED, as opposed to the snippet being broken. A bare
# non-zero exit (`raise SystemExit(1)`) prints no traceback at all and also qualifies.
#
# Everything else fails CLOSED. A denylist of known-bad markers was the wrong shape here:
# the reviewer has to JSON-escape its Python, so a mangled newline gives `IndentationError`
# and exit 1, and the sandbox has no filesystem or network, so `open(...)` gives
# `FileNotFoundError` and exit 1. Under a denylist each of those laundered a BROKEN
# snippet into the strongest evidence class and told the caller it was the only objection
# that established anything. An allowlist inverts that: an exception nobody recognised is
# a snippet that did not run, never a refutation.
_GENUINE_FAILURES = ("AssertionError", "SystemExit")

# Python prints the exception type on the LAST line of a traceback, so that is what is
# read. `_EXC_LINE` matches a bare `SomeError` or `pkg.SomeError: detail`.
_EXC_LINE = re.compile(r"^(?:[A-Za-z_][\w.]*\.)?([A-Za-z_]\w*(?:Error|Exit|Exception|Interrupt))\b")


def _failure_kind(stderr: str) -> str:
    """``"check"`` when a non-zero exit means the draft's claim broke, ``"snippet"`` when
    it means the code did.

    Reads the LAST exception line of the traceback, because that is where Python names
    the exception, and because a snippet that floods stderr can push it out of a
    head-truncated capture (see ``sandbox`` — the capture keeps both ends for this)."""
    for line in reversed((stderr or "").splitlines()):
        match = _EXC_LINE.match(line.strip())
        if match:
            return "check" if match.group(1) in _GENUINE_FAILURES else "snippet"
    return "check"  # no traceback at all: a deliberate non-zero exit


async def _execute(objections: list[dict], gate_ok: bool, budget_s: float = _RUN_BUDGET_S) -> None:
    """Run each `run` receipt's code and stamp the clerk's verdict on it, in place.

    This is the only thing that may write ``clerk_verdict``; it is stripped from anything
    a model sends. Until it runs, a `run` receipt is a CLAIM about a check, and claims
    score nothing — which is the difference between the reviewer telling us its check
    fails and the check actually failing.

    Exit non-zero means the draft's claim does not hold: an objection. Exit zero is
    evidence FOR the draft, not against it. A disabled or broken sandbox, a timeout, or a
    resource limit is inconclusive — never a finding, so a sandbox failure cannot
    masquerade as evidence.
    """
    started = time.monotonic()
    for obj in objections:
        receipt = obj.get("receipt")
        # Same normalization the classifier uses, so the predicate deciding whether code
        # RUNS and the one deciding what it MEANS can never disagree.
        if not isinstance(receipt, dict) or str(receipt.get("kind") or "").strip().lower() != "run":
            continue
        code = str(receipt.get("code") or "")
        if not gate_ok or not code.strip():
            receipt["clerk_verdict"] = "inconclusive"
            continue
        if time.monotonic() - started > budget_s:
            # Twelve receipts at the per-run timeout is minutes inside one tool call, past
            # most clients' patience. Anything unrun is inconclusive, never a finding.
            receipt["clerk_verdict"] = "inconclusive"
            continue
        res = await sandbox.run_python(code)
        if res.status in ("disabled", "unavailable", "timeout", "error"):
            receipt["clerk_verdict"] = "inconclusive"
        elif res.status == "ok":
            receipt["clerk_verdict"] = "passed"
        elif _failure_kind(res.stderr or "") == "check":
            receipt["clerk_verdict"] = "failed"
        else:
            receipt["clerk_verdict"] = "inconclusive"  # the snippet broke, not the draft


def extract(text: str) -> tuple[str, list[dict], bool]:
    """``(prose_without_block, objections, block_read)`` from a reviewer turn.

    ``block_read`` distinguishes "the reviewer emitted a readable block that happened to
    be empty" — a clean bill of health — from "nothing parseable came back", which may be
    findings written as prose. Collapsing those two into an empty list told the caller
    the reviewer found nothing when it may have found plenty.

    Same two-tier, fail-safe rules as the debate and falsify ledgers: prefer an explicit
    ``json-verify`` fence, else a plain block carrying the sentinel; strip EVERY
    identified block so none leaks into prose, but trust the value only when exactly one
    block is present — two blocks means the reviewer produced something we cannot read
    unambiguously, and guessing which one counts is how a wrong objection gets authority.
    """
    text = text or ""
    parsed = [(m, dl._loose_json(m.group(2))) for m in dl._FENCE_RE.finditer(text)]
    fenced = [(m, obj) for (m, obj) in parsed if m.group(1).strip().lower() == FENCE]
    candidates = fenced or [
        (m, obj) for (m, obj) in parsed if isinstance(obj, dict) and SENTINEL in obj
    ]
    if not candidates:
        return text.strip(), [], False
    prose_parts: list[str] = []
    cur = 0
    for m, _ in sorted(candidates, key=lambda c: c[0].start()):
        prose_parts.append(text[cur : m.start()])
        cur = m.end()
    prose_parts.append(text[cur:])
    prose = re.sub(r"\n{3,}", "\n\n", "".join(prose_parts)).strip()
    if len(candidates) != 1:
        return prose, [], False
    block = candidates[0][1]
    if not isinstance(block, dict):
        return prose, [], False
    raw = block.get("objections")
    if not isinstance(raw, list):
        return prose, [], False
    return prose, scrub(raw), True


def scrub(objections: list) -> list[dict]:
    """Model-written objections with every clerk-only verdict removed.

    The reviewer says what it found and quotes its evidence; whether that evidence holds
    is decided downstream by code. Stripping here rather than ignoring later means a
    stray ``"backed": true`` cannot survive into the result by some path nobody thought
    to filter.
    """
    out: list[dict] = []
    for item in objections[:_MAX_OBJECTIONS]:
        if not isinstance(item, dict):
            continue
        clean = {k: v for k, v in item.items() if k not in CLERK_FIELDS}
        receipt = clean.get("receipt")
        if isinstance(receipt, dict):
            clean["receipt"] = {k: v for k, v in receipt.items() if k not in CLERK_FIELDS}
        out.append(clean)
    return out


async def _handle_verify(args: dict) -> dict:
    """Review a draft answer and return it with classified objections attached.

    The draft is returned on EVERY path, including a reviewer that errors, refuses or
    returns nothing. v1 has no way to withhold it.
    """
    from .server import (  # local import: these live in server.py, avoid a cycle
        _add_refs,
        _add_thinking,
        _dump_sources,
        _group_slot_detail,
        _guard_check,
        _guard_refusal,
        _hub_mirror,
        _prepare_context,
        _resolve_one,
        _resolve_trusted,
    )

    question = str(args.get("question") or "").strip()
    if not question:
        return {"status": "error", "kind": "bad_args", "detail": "ask_verify needs a `question`"}
    draft = str(args.get("answer") or "").strip()
    if not draft:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": "ask_verify needs an `answer` — the draft to review. To have a "
            "model produce AND check its own answer, use ask_debate or ask_falsify.",
        }
    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    if ref_fail is not None:
        return ref_fail

    detail = _group_slot_detail("the reviewer", str(args.get("reviewer") or ""))
    if detail is not None:
        return {"status": "error", "kind": "bad_args", "detail": detail}
    reviewer = _resolve_one(str(args.get("reviewer") or ""), "opus")
    if reviewer is None:
        return {"status": "error", "kind": "no_models", "detail": "unrecognized reviewer model"}
    # A named drafter lets us refuse a same-lab review: a family grading its own homework
    # agrees with itself for reasons that have nothing to do with the draft being right.
    drafter = str(args.get("drafted_by") or "").strip()
    if drafter:
        drafter_key = _resolve_one(drafter, "")
        if drafter_key is None:
            # Fail closed. Silently skipping the guard on an unrecognized name leaves the
            # caller believing they opted into a cross-lab review they did not get — and
            # an unknown `reviewer` is already a hard error, so this was asymmetric too.
            return {
                "status": "error",
                "kind": "no_models",
                "detail": f"unrecognized drafted_by model {drafter!r} — name a known "
                "oracle so the same-lab check can run, or omit it",
            }
        if oracles.lab_of(drafter_key) == oracles.lab_of(reviewer):
            return {
                "status": "error",
                "kind": "bad_args",
                "detail": f"reviewer and drafter must be different labs (both are "
                f"{oracles.lab_of(reviewer)!r}); a model must not grade its own family",
            }

    allowed, reason = _guard_check(question, f"{context}\n{draft}", trusted=_resolve_trusted(args))
    if not allowed:
        return _guard_refusal(reason)

    trace_runtime.set_orchestration(mode="verify", reviewer=reviewer)
    rep = Reporter(f"ask_verify · reviewed by {oracles.label(reviewer)}")
    # Advertise the executable receipt only when the sandbox can really run one, and
    # self-test it first — the same gate the falsification clerk uses. Timed BEFORE `t0`:
    # the self-test spawns bwrap twice, and charging that to the review's latency makes
    # every audit row and hub turn overstate how long the model took.
    run_ready = sandbox.enabled() and sandbox.available()
    gate_ok = run_ready and await sandbox.self_test()
    if run_ready and not gate_ok:
        rep.warn("code execution is on but the sandbox self-test failed — checks stay unrun")
    t0 = time.monotonic()
    rep.start(f"{oracles.label(reviewer)} reviewing the draft")

    res = await oracles.run(
        reviewer, compose_verify(question, draft, context, run_enabled=gate_ok)
    )
    secs = time.monotonic() - t0

    objections: list[dict] = []
    review_error: dict | None = None
    prose = ""
    unreadable = False
    if res.status == "ok":
        prose, objections, block_read = extract(res.text or "")
        # A reviewer that wrote its findings as prose and forgot the fence, or emitted two
        # blocks (which `extract` refuses to trust), lands here with nothing parsed. That
        # must not read as "found nothing" — the findings may be real and are otherwise
        # unrecoverable from the result. An EMPTY but readable block is a real clean bill
        # of health and is not flagged.
        unreadable = bool(not block_read and (res.text or "").strip())
        rep.ok(f"{oracles.label(reviewer)} returned {len(objections)} objection(s)", secs)
    else:
        # The reviewer failing is not the draft failing. Say the review did not happen and
        # hand back the draft — the caller is strictly no worse off than before they asked.
        review_error = {
            "model": oracles.label(reviewer),
            "status": res.status,
            "kind": res.kind or res.status,
            "detail": (res.text or "").strip()[:300] or "no text returned",
        }
        rep.fail(f"review unavailable ({res.kind or res.status})", secs)

    # Execute before classifying: `executed` is assignable only from a verdict written
    # here, so a check that was never run cannot reach the strongest class.
    await _execute(objections, gate_ok)
    # AFTER the checks run: executing them is part of what the call spent, and leaving it
    # out meant the expensive half was counted nowhere.
    duration_ms = int((time.monotonic() - t0) * 1000)
    # CODE assigns every class. The reviewer's own opinion of its evidence was stripped at
    # ingest; this is where an objection earns its weight or fails to.
    for obj in objections:
        obj["receipt_class"] = verify_metrics.classify(obj, draft=draft, corpus=context)
    metrics = verify_metrics.report(objections, draft=draft)

    rep.footer(
        f"done — {metrics['prevented']} backed, {metrics['unbacked_objections']} unbacked"
    )
    audit.record(
        decision="allowed" if review_error is None else "error",
        stage=None,
        reason=metrics["verdict"],
        question=question,
        context=context,
        session="verify",
        model=oracles.label(reviewer),
        duration_ms=duration_ms,
    )
    saved = outputs.save(
        tool="ask_verify",
        model=oracles.label(reviewer),
        question=question,
        answer=draft,
        context=context,
        session="verify",
        thinking=res.thinking,
        sources=_dump_sources([res]),
    )
    if review_error is None:
        # `_hub_mirror` is documented as success-path only; mirroring a failed review as
        # `ok` would have hub health count a review that never happened.
        _hub_mirror(
            session_key=str(args.get("session") or "verify"),
            question=question,
            answer=draft,
            oracle=oracles.label(reviewer),
            status="ok",
            duration_ms=duration_ms,
        )

    payload: dict = {
        "status": "ok",
        "mode": "verify",
        # The draft, unchanged and unconditional. v1 cannot withhold it.
        "answer": draft,
        "reviewed_by": oracles.label(reviewer),
        "objections": objections,
        "verify": metrics,
        "saved": saved,
        **(
            {"block_unreadable": True, "reviewer_prose": prose[:4000]}
            if unreadable
            else {}
        ),
        "recommended_next_action": _next_action(metrics, review_error, unreadable),
    }
    if review_error is not None:
        payload["review_error"] = review_error
    _add_refs(payload, ref_resolved, ref_missing)
    _add_thinking(payload, res.thinking)
    return payload


def _next_action(metrics: dict, review_error: dict | None, unreadable: bool = False) -> str:
    """What the caller should do, stated so that 'no objections' can never read as
    'verified'. Nothing here was proved correct; some things were shown wrong."""
    if review_error is not None:
        return (
            "the review did not run — this draft is UNREVIEWED, exactly as it was before "
            "you called; re-run or treat it as unchecked"
        )
    if unreadable:
        return (
            "the reviewer wrote something but no readable objections block — read "
            "`reviewer_prose` yourself; this is NOT 'it found nothing'"
        )
    if metrics["prevented"]:
        return (
            f"{metrics['prevented']} objection(s) carry evidence code could check against "
            "your inputs or an executed check — read those first; they are the only ones "
            "that establish anything"
        )
    if metrics["verdict"] == "self-quoting":
        return (
            "every objection quoted the draft back at itself, which establishes nothing — "
            "treat this as NOT reviewed, and supply the source material as `context` so "
            "the reviewer has something to check against"
        )
    if metrics["unbacked_objections"]:
        return (
            f"{metrics['unbacked_objections']} objection(s) are argument without evidence — "
            "weigh them as opinion, not as findings"
        )
    return (
        "the reviewer raised nothing it could back with evidence; that is not a "
        "correctness guarantee — it means no fault was DEMONSTRATED, not that none exists"
    )


async def run_arena(items: list[dict]) -> list[dict]:
    """Run ``ask_verify`` over a seeded draft set and score each run — the acceptance
    harness. Makes REAL model calls, so it is an operator/eval helper, not a unit-tested
    path. Each item is an ``ask_verify`` arg dict plus a ``seeded`` flag marking whether
    the draft carries a known injected fault.

    This is the only judge-free signal for whether objections are RIGHT. The in-tool
    metrics measure effect and attribution; they cannot measure direction, because a
    confident wrong objection counts exactly like a true one.
    """
    results: list[dict] = []
    for item in items or []:
        seeded = bool(item.get("seeded"))
        args = {k: v for k, v in item.items() if k != "seeded"}
        out = await _handle_verify(args)
        # A failed review is NOT "the reviewer found nothing". `_handle_verify` returns
        # `ok` with the draft even when the reviewer errors, so scoring on status alone
        # counted a timed-out backend as a clean miss — poisoning the one number the
        # suppression gate is judged on.
        if out.get("status") != "ok" or out.get("review_error"):
            results.append(
                {
                    "label": item.get("label"),
                    "seeded": seeded,
                    "status": "review_failed" if out.get("review_error") else out.get("status"),
                    "detail": out.get("detail")
                    or out.get("reason")
                    or (out.get("review_error") or {}).get("detail"),
                }
            )
            continue
        results.append(
            {
                "label": item.get("label"),
                "seeded": seeded,
                "status": "ok",
                **out["verify"],
            }
        )
    return results


def arena_report(results: list[dict]) -> dict:
    """Precision on seeded faults and false-objection rate on clean drafts."""
    ok = [r for r in results or [] if r.get("status") == "ok"]
    return {
        "runs": len(results or []),
        "scored": len(ok),
        **verify_metrics.seeded_scores(ok),
    }
