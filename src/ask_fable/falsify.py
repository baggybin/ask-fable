"""``ask_falsify`` orchestration — the stateful cousin of ``ask_debate``.

An assertor states typed claims; a falsifier (forced to a DIFFERENT lab) attacks them;
and a deterministic CODE clerk — not a model — decides commit / kill / survive from
receipts it verifies mechanically (a ``cite`` quote's verbatim presence in the shared
context, a ``contra`` edge to a survived claim). A claim may speak, but it cannot
compound without a receipt. State persists across calls under ``falsify/<session>/ledger``
in the context bus, so a killed claim stays dead and the topology of who asserts moves
with outcomes.

The pure decision logic lives in :mod:`falsify_ledger`; this module does the I/O
(context-bus load/store), the model turns, and the reputation bookkeeping, then calls in.

v1 receipts: ``cite`` (presence) and ``contra`` (edge). ``run:`` execution and
``metamorph:`` re-runs are deferred (see the change plan); the ledger already models
their receipt shapes.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from . import (
    audit,
    context_store,
    falsify_metrics,
    oracles,
    outputs,
    reputation,
    sandbox,
    sidecar,
    trace_runtime,
    worker,
)
from . import falsify_ledger as fl
from .console import Reporter
from .prompts import compose_falsify_step

_FALSIFY_LOCK_WAIT_S = 2.0  # fail fast rather than block a sibling for a multi-round run

def _ledger_key(session: str) -> str:
    return f"falsify/{session}/ledger"


def _load_ledger(session: str) -> tuple[dict, int] | None:
    """Load the persisted ledger as ``(ledger, version)``; ``None`` means the store is
    degraded. The version feeds the optimistic-concurrency save (local backend) so a
    concurrent writer can't silently clobber this run; on the bus it is 0 (newest-wins).

    A degraded read must NEVER become a fresh ledger: the next save would overwrite a
    possibly-intact (and, on the LAN bus, possibly shared) ledger with an empty one. Only a
    genuinely absent key starts a new ledger."""
    row = context_store.get_versioned(_ledger_key(session))
    if row is None:
        if context_store.last_error() is not None:
            return None
        return fl.new_ledger(session), 0
    raw, version = row
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and obj.get("falsify_version"):
            return obj, version
    except (json.JSONDecodeError, ValueError):
        pass
    return fl.new_ledger(session), version


def _save_ledger(session: str, ledger: dict, expected_version: int | None = None) -> bool:
    """Persist the ledger; False when the store refused the write — a real store error, OR
    (with ``expected_version``, local backend) a concurrent writer changed it first. The
    caller surfaces False as `not persisted`."""
    return context_store.put(
        _ledger_key(session), json.dumps(ledger), description="ask_falsify ledger",
        expected_version=expected_version,
    )


def _turn_rows(block: dict | None, field: str) -> list[dict]:
    if not isinstance(block, dict):
        return []
    rows = block.get(field)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


async def _falsify_turn(key: str, role: str, question: str, corpus: str, rep,
                        *, ledger_text: str, run_enabled: bool = False) -> dict:
    """Run one assert/falsify turn; split its text into prose + the json-falsify block."""
    rep.start(f"{role}: {oracles.label(key)}")
    t0 = time.monotonic()
    res = await oracles.run(
        key, compose_falsify_step(question, role, ledger=ledger_text, run_enabled=run_enabled), corpus
    )
    trace_runtime.record_stage(
        "orchestration.provider", res.status,
        kind=trace_runtime.EventKind.ORCHESTRATION,
        orchestration={"mode": "falsify", "role": role, "provider": res.model},
    )
    secs = time.monotonic() - t0
    if res.status == "ok":
        prose, _sc = sidecar.extract(res.text)
        prose, block = fl.extract(prose)
        rep.ok(f"{role} answered", secs)
    else:
        prose, block = res.text, None
        rep.warn(f"{role} {res.status}: {res.text}", secs)
    rep.think(f"{res.model} ({role})", res.thinking)
    return {"key": key, "model": res.model, "role": role, "res": res, "prose": prose, "block": block}


_INCONCLUSIVE_MARKERS = ("MemoryError", "ModuleNotFoundError", "can't start new thread", "Errno 12")


async def _run_verdict(code: str, gate_ok: bool) -> str:
    """Execute a model-authored ``run`` snippet and return 'pass' | 'fail' | 'inconclusive'.
    A disabled/broken sandbox, a resource-limit hit, a timeout, or a missing module is
    INCONCLUSIVE — never a refutation — so a sandbox failure can't masquerade as evidence."""
    if not gate_ok:
        return "inconclusive"
    res = await sandbox.run_python(code or "")
    if res.status in ("disabled", "unavailable", "timeout", "error"):
        return "inconclusive"
    if res.status == "fail" and any(m in (res.stderr or "") for m in _INCONCLUSIVE_MARKERS):
        return "inconclusive"
    return "pass" if res.status == "ok" else "fail"


async def _clerk_asserts(ledger: dict, block: dict | None, author: str, corpus: str,
                         gate_ok: bool) -> tuple[dict, list]:
    """Ingest the assertor's claims: stamp author, strip any verdict the model wrote itself,
    drop illegal re-asserts, and verify each NEW receipt — ``cite`` by verbatim presence,
    ``run`` by executing it (exit 0 supports the claim). The clerk is code, never a model."""
    raws = _turn_rows(block, "claims")
    for r in raws:
        r["author"] = author
        # A model can't certify its own evidence: a self-written `ok`/`stable` (or a whole
        # "stable" metamorph receipt) let an unbacked claim survive with no check run.
        r["receipts"] = fl.scrub_receipts(r.get("receipts"))
    held = {cid: len(c.get("receipts") or []) for cid, c in fl.by_id(ledger).items()}
    ledger, accepted, rejected = fl.apply_asserts(ledger, raws)
    for c in fl.claims(ledger):
        if c["id"] not in accepted:
            continue
        # Only this turn's receipts (apply_asserts appends them). An earlier receipt keeps
        # the verdict it earned: re-checking it against a later call's corpus flipped a
        # verified cite to ok:false and left a survived claim to die on one challenge.
        for receipt in c.get("receipts", [])[held.get(c["id"], 0):]:
            if receipt.get("kind") == "cite":
                receipt["ok"] = fl.cite_supported(receipt, corpus)
            elif receipt.get("kind") == "run":
                verdict = await _run_verdict(str(receipt.get("code") or ""), gate_ok)
                receipt["ok"] = verdict == "pass"
                if verdict == "inconclusive":
                    receipt["inconclusive"] = True
    return ledger, rejected


async def _clerk_attacks(ledger: dict, block: dict | None, killer: str, corpus: str,
                         round_no: int, gate_ok: bool) -> dict:
    """Resolve the falsifier's attacks mechanically. A CHALLENGE against an unsupported
    (fabricated/absent cite) claim kills it; against a supported claim it fails and counts
    toward survival. A CONTRA registers a cited new claim contradicting a survived one; a
    RUN executes the falsifier's test — a non-zero exit proves the claim false and kills it.
    An attack on an already-killed claim is ignored: it can't die twice."""
    for atk in _turn_rows(block, "attacks"):
        target_id = str(atk.get("target") or "").strip()
        target = fl.by_id(ledger).get(target_id)
        # Re-killing a dead claim re-bumped kills/deaths every time it was challenged,
        # inflating reputation and kill_provenance/spark_density; skip it outright.
        if target is None or target.get("status") == "killed":
            continue
        domain = str(target.get("domain") or "general")
        author = str(target.get("author") or "")
        move = str(atk.get("move") or "challenge")
        if move == "contra":
            new_id = str(atk.get("new_id") or "").strip()
            quote = str(atk.get("quote") or "")
            present = bool(quote) and quote in (corpus or "")
            # The contradicting claim must be NEW and actually registered. Reusing an id
            # grafted the falsifier's quote onto the assertor's own claim, or — naming a
            # killed claim, whose re-assert is rejected — still drew the killing edge.
            if new_id and present and new_id not in fl.by_id(ledger):
                ledger, acc, _rej = fl.apply_asserts(ledger, [{
                    "id": new_id, "author": killer, "domain": atk.get("domain") or domain,
                    "claim": atk.get("claim") or f"contradicts {target_id}",
                    "receipts": [{"kind": "cite", "quote": quote, "ok": True}],
                }])
                if new_id in acc and fl.contra_valid(new_id, target_id, ledger):
                    ledger = fl.add_contra_edge(ledger, new_id, target_id)
                    ledger = fl.record_kill(ledger, target_id, by=killer, kind="contra",
                                            accepted=True, round_no=round_no)
                    ledger = fl.bump_rep(ledger, killer, domain, "kills")
                    ledger = fl.bump_rep(ledger, author, domain, "deaths")
                    continue
            ledger = fl.record_kill(ledger, target_id, by=killer, kind="contra",
                                    accepted=False, round_no=round_no)
            continue
        if move == "run":
            verdict = await _run_verdict(str(atk.get("code") or ""), gate_ok)
            if verdict == "fail":       # the falsifier's executable test found the claim false
                ledger = fl.record_kill(ledger, target_id, by=killer, kind="run",
                                        accepted=True, round_no=round_no)
                ledger = fl.bump_rep(ledger, killer, domain, "kills")
                ledger = fl.bump_rep(ledger, author, domain, "deaths")
            elif verdict == "pass":     # test passed → the claim withstands the attempt
                ledger = fl.record_kill(ledger, target_id, by=killer, kind="run",
                                        accepted=False, round_no=round_no)
            # inconclusive → no attempt recorded (a broken sandbox mustn't help either side)
            continue
        # CHALLENGE: an unsupported claim dies; a supported one withstands the attempt.
        supported = fl.compoundable(fl.by_id(ledger)[target_id])
        ledger = fl.record_kill(ledger, target_id, by=killer, kind="challenge",
                                accepted=not supported, round_no=round_no)
        if not supported:
            ledger = fl.bump_rep(ledger, killer, domain, "kills")
            ledger = fl.bump_rep(ledger, author, domain, "deaths")
    return ledger


_METAMORPH_PERTURB = (
    "Restate the CLAIM below in a semantics-PRESERVING way: rename any entities, reorder "
    "clauses, swap in synonyms, change the surface form — but keep its truth-value IDENTICAL. "
    "Output ONLY the restated claim on one line, no preamble.\n\nCLAIM: {claim}"
)
_METAMORPH_JUDGE = (
    "Judge the following proposition ON ITS OWN MERITS, from scratch. Reply with EXACTLY ONE "
    "WORD — TRUE, FALSE, or UNSURE — and nothing else.\n\nPROPOSITION: {prop}"
)
# Cheap cross-lab models used only as a FALLBACK paraphraser (see `_paraphrase`).
_PERTURBERS = ("minimax", "deepseek", "glm")


async def _paraphrase(claim_text: str, assertor: str) -> str | None:
    """A semantics-preserving reword of the claim by a model that is NOT the assertor,
    so the cold re-judge can't just recognize the assertor's own phrasing.

    Prefers the Haiku worker: it is always available on the OAuth session, costs
    nothing beyond the flat plan, and is a different model than any assertor — which
    removes the old fragility where, with no cheap cross-lab model configured, the
    perturber fell back to the ASSERTOR itself and the check became a model
    paraphrasing then re-judging its own words (trivially stable, useless). Falls back
    to a cheap cross-lab oracle, and returns None (skip this round, retry next) rather
    than ever letting the assertor perturb its own claim."""
    prompt = _METAMORPH_PERTURB.format(claim=claim_text)
    r = await worker.run(prompt)
    if r.status == "ok" and (r.text or "").strip():
        return r.text.strip().splitlines()[0][:500]
    # Haiku unavailable (e.g. too-old Claude Code) — a cheap cross-lab model, not the assertor.
    for m in _PERTURBERS:
        if m != assertor and oracles.available(m):
            r = await oracles.run(m, prompt)
            if r.status == "ok" and (r.text or "").strip():
                return r.text.strip().splitlines()[0][:500]
    return None


def _parse_verdict(text: str) -> str:
    """Read the judge's one-word verdict. Word-boundary matched, and a negated
    affirmative ("UNTRUE", "NOT TRUE") resolves to FALSE — a plain substring scan read
    the "TRUE" inside "UNTRUE" and certified a refuted claim as stable."""
    up = (text or "").upper()
    if re.search(r"\bUNTRUE\b", up) or re.search(r"\bNOT\s+TRUE\b", up):
        return "FALSE"
    for token in ("FALSE", "UNSURE", "TRUE"):
        if re.search(rf"\b{token}\b", up):
            return token
    return "UNSURE"


async def _metamorph_check(claim_text: str, assertor: str) -> dict | None:
    """Perturb the claim (semantics-preserving) and re-ask the ASSERTING model COLD whether it
    still holds. Stable iff it reaffirms TRUE. Instability is a proof of NON-REASONING, not of
    falsity — so it only withholds support (default-deny), it never kills. Returns a metamorph
    receipt, or None when the check itself couldn't run (no paraphraser, or a model error →
    retry next round)."""
    if not claim_text.strip():
        return None
    para = await _paraphrase(claim_text, assertor)
    if not para:
        return None
    j = await oracles.run(assertor, _METAMORPH_JUDGE.format(prop=para))  # cold: no context, no ledger
    if j.status != "ok":
        return None
    verdict = _parse_verdict(j.text)
    # `by: clerk` is the only provenance resolve() accepts for a metamorph receipt.
    return {"kind": "metamorph", "stable": verdict == "TRUE", "paraphrase": para,
            "verdict": verdict, "by": "clerk"}


async def _clerk_metamorph(ledger: dict, assertor: str) -> dict:
    """For each OPEN, currently-unsupported claim without a clerk metamorph receipt, run the
    check and append its receipt. A stable result then counts as (weak) support at resolve
    time — the only way a claim survives on a topic with no corpus to cite and no code to run.
    Stability is NOT truth; the output flags metamorph-only survivors as `stable_unverified`."""
    for c in fl.claims(ledger):
        if c.get("status") != "open" or fl.compoundable(c):
            continue
        if any(isinstance(r, dict) and r.get("kind") == "metamorph" and r.get("by") == "clerk"
               for r in c.get("receipts", [])):
            continue
        receipt = await _metamorph_check(str(c.get("claim") or ""), assertor)
        if receipt is not None:
            c.setdefault("receipts", []).append(receipt)
    return ledger


def _credit_survivors(before: dict, after: dict) -> dict:
    """Award a `survives` credit to the author of every claim that newly reached
    `survived` this round."""
    was = set(fl.survived_ids(before))
    for cid in fl.survived_ids(after):
        if cid not in was:
            c = fl.by_id(after).get(cid) or {}
            after = fl.bump_rep(after, str(c.get("author") or ""), str(c.get("domain") or "general"), "survives")
    return after


async def _handle_falsify(args: dict) -> dict:
    """Advance a persistent falsification ledger by ``rounds`` assert→attack cycles.
    ``session`` is REQUIRED — it is the ledger's persistence key, not just hub grouping."""
    from .server import (  # local import: these live in server.py, avoid a cycle at import time
        _group_slot_detail,
        _guard_check,
        _guard_refusal,
        _prepare_context,
        _resolve_one,
        _resolve_trusted,
        _session_lock,
    )

    question = str(args.get("question") or "").strip()
    if not question:
        return {"status": "error", "kind": "bad_args", "detail": "ask_falsify needs a `question`"}
    session = str(args.get("session") or "").strip()
    if not session:
        return {"status": "error", "kind": "bad_args",
                "detail": "ask_falsify needs a `session` — it is the ledger's persistence key"}
    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    if ref_fail is not None:
        return ref_fail
    for role in ("assertor", "falsifier"):
        detail = _group_slot_detail(f"the {role}", str(args.get(role) or ""))
        if detail is not None:
            return {"status": "error", "kind": "bad_args", "detail": detail}
    assertor = _resolve_one(str(args.get("assertor") or ""), "minimax")
    falsifier = _resolve_one(str(args.get("falsifier") or ""), "opus")
    if assertor is None or falsifier is None:
        miss = "assertor" if assertor is None else "falsifier"
        return {"status": "error", "kind": "no_models", "detail": f"unrecognized {miss} model"}
    # Same-lab pairs grade their own homework. Lab identity comes from oracles — the
    # single source of truth — so Anthropic variants (sonnet/opus48/fable51) and gateway
    # tokens are merged into their family, not just the base fable/opus pair.
    if oracles.lab_of(assertor) == oracles.lab_of(falsifier):
        return {"status": "error", "kind": "bad_args",
                "detail": f"assertor and falsifier must be different labs (both are "
                          f"{oracles.lab_of(assertor)!r}); a model must not grade its own family"}
    try:
        rounds = max(1, min(int(args.get("rounds") or 1), 6))
    except (TypeError, ValueError):
        rounds = 1

    allowed, reason = _guard_check(question, context, trusted=_resolve_trusted(args))
    if not allowed:
        return _guard_refusal(reason)

    metamorph = bool(args.get("metamorph"))
    trace_runtime.set_orchestration(mode="falsify", assertor=assertor, falsifier=falsifier, rounds=rounds)
    # Serialize same-session runs: the ledger is a whole-blob read-modify-write spanning
    # minutes of model calls, so two overlapping calls on one session clobber each other's
    # claims. Fail fast — don't make a sibling wait minutes (agents don't coordinate). This is
    # PER-PROCESS only; a second ask_fable process is not covered (the ledger CAS is the
    # follow-up), but the reputation double-count is fixed by the store's idempotency key.
    lock = _session_lock("falsify:" + session)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_FALSIFY_LOCK_WAIT_S)
    except TimeoutError:
        return {"status": "error", "kind": "session_busy",
                "detail": f"another ask_falsify run holds session '{session}'; retry shortly"}
    try:
        return await _falsify(question, context, assertor, falsifier, rounds, session,
                              ref_resolved or [], ref_missing or [], metamorph=metamorph)
    finally:
        lock.release()


async def _falsify(question: str, corpus: str, assertor: str, falsifier: str, rounds: int,
                   session: str, ref_resolved: list[str], ref_missing: list[str],
                   metamorph: bool = False) -> dict:
    from .server import _add_refs, _add_thinking, _dump_sources, _hub_mirror

    rep = Reporter(f"ask_falsify · {oracles.label(assertor)} vs {oracles.label(falsifier)}")
    loaded = _load_ledger(session)
    if loaded is None:
        return {
            "status": "error",
            "kind": "ledger_unavailable",
            "detail": (
                f"the falsification ledger for session '{session}' could not be read "
                f"({context_store.last_error()}); refusing to start a fresh ledger over "
                "a possibly-intact one — retry once the context store is healthy"
            ),
        }
    ledger, ledger_version = loaded
    persist_ok = True
    t0 = time.monotonic()
    turns: list[dict] = []
    a_label, f_label = oracles.label(assertor), oracles.label(falsifier)
    # run: receipts execute model code — advertise them only when the sandbox is usable, and
    # self-test the sandbox once so an env quirk can't turn a broken run into a false kill.
    run_ready = sandbox.enabled() and sandbox.available()
    gate_ok = run_ready and await sandbox.self_test()

    for _ in range(rounds):
        # `fixpoint` describes the round that set it, not the ledger forever: a later call
        # that adds or kills a claim must report (and persist) `active` again.
        ledger["status"] = "active"
        before = json.loads(json.dumps(ledger))  # cheap deep snapshot for fixpoint/survivor diff
        round_no = int(ledger.get("round", 0)) + 1
        at = await _falsify_turn(assertor, "assert", question, corpus, rep,
                                 ledger_text=fl.render_ledger(ledger), run_enabled=run_ready)
        turns.append(at)
        if at["res"].status != "ok" and not turns[:-1]:
            # the very first turn failed with nothing accrued — surface it
            if at["res"].status == "refused":
                return {"status": "refused", "stage": "model", "reason": at["res"].text}
            return {"status": "error", "kind": at["res"].kind, "detail": at["res"].text}
        ledger, _rejected = await _clerk_asserts(ledger, at["block"], a_label, corpus, gate_ok)
        if metamorph:
            ledger = await _clerk_metamorph(ledger, assertor)

        ft = await _falsify_turn(falsifier, "falsify", question, corpus, rep,
                                 ledger_text=fl.render_ledger(ledger), run_enabled=run_ready)
        turns.append(ft)
        ledger = await _clerk_attacks(ledger, ft["block"], f_label, corpus, round_no, gate_ok)

        ledger = fl.resolve(ledger)
        ledger = _credit_survivors(before, ledger)
        ledger["round"] = round_no
        # Optimistic-concurrency save: on the local backend a False means a concurrent writer
        # changed the ledger (or a store error), so persist_ok drops and the caller says so
        # rather than the run believing it saved. On success the version advances.
        if fl.is_fixpoint(before, ledger):
            ledger["status"] = "fixpoint"
            if _save_ledger(session, ledger, ledger_version):
                ledger_version += 1
            else:
                persist_ok = False
            break
        if _save_ledger(session, ledger, ledger_version):
            ledger_version += 1
        else:
            persist_ok = False

    # Feed resolved outcomes into the persistent calibration store. Idempotency now lives in
    # the store's durable (session, claim) key, so this no longer mutates or re-saves the
    # ledger. Only record once the ledger itself is durable — a failed save means a retry
    # re-derives the same outcomes (and the key dedupes them).
    if persist_ok:
        reputation.record_ledger_outcomes(ledger)

    duration_ms = int((time.monotonic() - t0) * 1000)
    answer = fl.render_ledger(ledger)
    head_thinking = next((t["res"].thinking for t in turns if t["res"].status == "ok" and t["res"].thinking), "")
    rep.footer(f"round {ledger.get('round')} — {ledger.get('status')}")

    audit.record(decision="allowed", stage=None, reason="falsify", question=question,
                 context=corpus, session="falsify", model="falsify", duration_ms=duration_ms,
                 quorum=str(ledger.get("status")))
    saved = outputs.save(tool="ask_falsify", model=f"{a_label} vs {f_label}", question=question,
                         answer=answer, context=corpus, session="falsify",
                         thinking=head_thinking, sources=_dump_sources([t["res"] for t in turns]))
    _hub_mirror(session_key=session, question=question, answer=answer,
                oracle=f"{a_label} vs {f_label}", status="ok", duration_ms=duration_ms)

    payload = {
        "status": "ok",
        "mode": "falsify",
        "session": session,
        "round": int(ledger.get("round", 0)),
        "ledger_status": str(ledger.get("status", "active")),
        "falsify": fl.summary_block(ledger),
        "answer": answer,
        "turns": [{"role": t["role"], "model": t["model"], "status": t["res"].status} for t in turns],
        "saved": saved,
    }
    if not persist_ok:  # the round ran but the ledger could not be written — say so
        rep.warn(f"ledger save failed: {context_store.last_error()}")
        payload["ledger_persisted"] = False
        payload["store_error"] = context_store.last_error()
    _add_refs(payload, ref_resolved, ref_missing)
    _add_thinking(payload, head_thinking)
    return payload


_ARENA_ARGS = ("question", "context", "context_ref", "session", "assertor", "falsifier")


async def run_arena(items: list[dict], *, rounds: int | None = None) -> list[dict]:
    """Run ``ask_falsify`` over a claim set and score each run with
    :func:`falsify_metrics.report` — the acceptance harness. Makes real model calls, so it
    is an operator/eval helper, not a unit-tested path. Each item is an ``ask_falsify`` arg
    dict (needs at least ``question`` + ``session``)."""
    results: list[dict] = []
    for item in items or []:
        args = {k: item[k] for k in _ARENA_ARGS if k in item}
        if rounds is not None:
            args["rounds"] = rounds
        out = await _handle_falsify(args)
        if out.get("status") != "ok":
            results.append({"session": item.get("session"), "status": out.get("status"),
                            "detail": out.get("detail") or out.get("reason")})
            continue
        loaded = _load_ledger(str(item.get("session") or ""))
        if loaded is None:
            results.append({"session": item.get("session"), "status": "error",
                            "detail": "ledger unavailable (context store degraded)"})
            continue
        ledger, _version = loaded
        rep = falsify_metrics.report(ledger, turns=len(out.get("turns", [])))
        results.append({"session": item.get("session"), "question": item.get("question"),
                        "status": "ok", **rep})
    return results
