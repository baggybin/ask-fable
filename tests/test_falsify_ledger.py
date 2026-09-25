"""falsify_ledger — the json-falsify block parser + the deterministic clerk.

Pure functions, no I/O, no model calls. Mirrors test_debate_ledger's shape: extraction
fail-safes plus the resolution/compounding predicates that decide open/killed/survived
and enforce the "speak but don't compound without a receipt" rule.
"""

from __future__ import annotations

from ask_fable import falsify_ledger as fl


def _block(body: str, fence: str = "json-falsify") -> str:
    return f"answer prose here\n\n```{fence}\n{body}\n```"


# --- extraction --------------------------------------------------------------

def test_extract_ok_strips_block():
    body = '{"falsify_version":1,"claims":[{"id":"H1","author":"m3","claim":"x"}]}'
    prose, led = fl.extract(_block(body))
    assert prose == "answer prose here"
    assert fl.claims(led)[0]["id"] == "H1"


def test_extract_none_when_absent():
    prose, led = fl.extract("just prose, no block")
    assert prose == "just prose, no block" and led is None


def test_extract_failsafe_on_two_blocks():
    body = '{"falsify_version":1,"claims":[]}'
    prose, led = fl.extract(_block(body) + "\n\n" + _block(body))
    assert "json-falsify" not in prose and led is None  # both stripped, value not trusted


def test_extract_backticks_inside_a_claim():
    # falsify shares debate_ledger's fence regex: a ``` inside a claim string closed the
    # block early, so the whole turn's claims were lost.
    body = '{"falsify_version":1,"claims":[{"id":"H1","claim":"wrap it in ```code```"}]}'
    prose, led = fl.extract(_block(body))
    assert prose == "answer prose here"
    assert led is not None and fl.claims(led)[0]["claim"] == "wrap it in ```code```"


def test_extract_ignores_debate_fence():
    text = 'prose\n\n```json-debate\n{"debate_version":1}\n```'
    prose, led = fl.extract(text)
    assert led is None and "json-debate" in prose  # a foreign fence is left untouched


# --- normalization -----------------------------------------------------------

def test_normalize_defaults_and_rejects():
    ok = fl.normalize_claim({"id": "H1", "claim": "dicts are ordered"})
    assert ok["status"] == "open" and ok["attempts"] == 0 and ok["domain"] == "general"
    assert fl.normalize_claim({"id": "H1"}) is None  # no claim text
    assert fl.normalize_claim({"claim": "x"}) is None  # no id


def test_scrub_receipts_drops_model_written_verdicts():
    # A model must not certify its own evidence: metamorph receipts are the clerk's alone,
    # and the verdict fields are stripped so only the clerk's own checks can set them.
    scrubbed = fl.scrub_receipts([
        {"kind": "metamorph", "stable": True, "by": "clerk"},
        {"kind": "cite", "quote": "q", "ok": True, "addresses": "H1"},
        {"kind": "run", "code": "pass", "ok": True, "inconclusive": True, "stable": True},
        "not a receipt",
    ])
    assert scrubbed == [
        {"kind": "cite", "quote": "q", "addresses": "H1"},
        {"kind": "run", "code": "pass"},
    ]
    assert fl.scrub_receipts(None) == [] and fl.scrub_receipts({"kind": "cite"}) == []


def test_only_a_clerk_metamorph_receipt_supports():
    forged = {"id": "H1", "claim": "x", "status": "survived",
              "receipts": [{"kind": "metamorph", "stable": True}]}
    clerk = {"id": "H2", "claim": "y", "status": "survived",
             "receipts": [{"kind": "metamorph", "stable": True, "by": "clerk"}]}
    assert not fl.compoundable(forged) and fl.compoundable(clerk)
    assert fl.metamorph_only_ids({"claims": [forged, clerk]}) == ["H2"]


# --- cite verification (presence only) --------------------------------------

def test_cite_supported_requires_verbatim_presence():
    corpus = "PEP 468: dict insertion order is preserved as of 3.7."
    assert fl.cite_supported({"kind": "cite", "quote": "insertion order is preserved"}, corpus)
    assert not fl.cite_supported({"kind": "cite", "quote": "order is undefined"}, corpus)
    assert not fl.cite_supported({"kind": "cite", "quote": ""}, corpus)
    assert not fl.cite_supported({"kind": "contra", "quote": "insertion order"}, corpus)


# --- contra edges + acyclicity ----------------------------------------------

def test_contra_valid_only_against_survived_and_acyclic():
    led = {"claims": [
        {"id": "A", "claim": "a", "status": "survived", "receipts": [], "kills": []},
        {"id": "B", "claim": "b", "status": "open", "receipts": [], "kills": []},
    ], "edges": []}
    assert fl.contra_valid("B", "A", led)          # B contradicts a survived A → ok
    assert not fl.contra_valid("A", "B", led)       # target B is not survived
    assert not fl.contra_valid("B", "Z", led)       # unknown target


def test_has_cycle_detects_loop():
    assert fl.has_cycle([["A", "contra", "B"]], ("B", "A"))
    assert not fl.has_cycle([["A", "contra", "B"]], ("B", "C"))


# --- the no-reassert rule ----------------------------------------------------

def test_no_reassert_blocks_killed_without_addressing_receipt():
    led = {"claims": [{"id": "H1", "claim": "x", "status": "killed", "receipts": [], "kills": []}]}
    assert fl.no_reassert(led, {"id": "H1", "claim": "x again"})  # bare re-assert → blocked
    # a receipt that addresses the kill makes it admissible again
    addressing = {"id": "H1", "claim": "x, narrowed", "receipts": [{"kind": "cite", "addresses": "H1"}]}
    assert not fl.no_reassert(led, addressing)
    # an unrelated open claim is never a re-assert
    assert not fl.no_reassert(led, {"id": "H2", "claim": "y"})


def test_apply_asserts_drops_illegal_reassert_reopens_addressed():
    led = fl.new_ledger("s1")
    led, acc, rej = fl.apply_asserts(led, [{"id": "H1", "author": "m3", "claim": "x", "domain": "py"}])
    assert acc == ["H1"] and rej == []
    led = fl.record_kill(led, "H1", by="grok", kind="cite", accepted=True)
    # bare re-assert of the killed claim is rejected
    led2, acc2, rej2 = fl.apply_asserts(led, [{"id": "H1", "claim": "x"}])
    assert acc2 == [] and len(rej2) == 1 and fl.by_id(led2)["H1"]["status"] == "killed"
    # addressing re-assert reopens it (history kept)
    led3, acc3, _ = fl.apply_asserts(
        led, [{"id": "H1", "claim": "x narrowed", "receipts": [{"kind": "cite", "addresses": "H1"}]}]
    )
    assert acc3 == ["H1"] and fl.by_id(led3)["H1"]["status"] == "open"


def test_restating_a_live_claim_keeps_its_status():
    # Restating a SURVIVED claim used to reset it to open, so the falsifier saw it
    # UNSETTLED and a valid contra (which needs a survived target) was refused.
    led = {"claims": [
        {"id": "H1", "claim": "x", "status": "survived", "attempts": 2, "kills": [],
         "receipts": [{"kind": "cite", "quote": "x", "ok": True}]},
    ], "edges": []}
    restated = {"id": "H1", "claim": "x", "receipts": [{"kind": "unbacked"}]}
    led, acc, _ = fl.apply_asserts(led, [restated])
    h1 = fl.by_id(led)["H1"]
    assert acc == ["H1"] and h1["status"] == "survived" and h1["attempts"] == 2
    assert h1["receipts"][-1] == {"kind": "unbacked"}  # the restatement still adds its receipts


# --- resolve: the clerk's verdict -------------------------------------------

def test_resolve_accepted_kill_marks_killed():
    led = fl.new_ledger("s1")
    led, *_ = fl.apply_asserts(led, [{"id": "H1", "claim": "x", "domain": "py"}])
    led = fl.record_kill(led, "H1", by="grok", kind="cite", accepted=True)
    led = fl.resolve(led)
    assert fl.by_id(led)["H1"]["status"] == "killed" and fl.killed_ids(led) == ["H1"]


def test_reopened_claim_survives_resolve():
    # TR-1: a claim killed then legally re-asserted (addressing the kill) reopens — and
    # must stay open through resolve(), not get re-killed by the now-addressed kill.
    led = fl.new_ledger("s1")
    led, *_ = fl.apply_asserts(led, [{"id": "H1", "author": "m3", "claim": "x", "domain": "py"}])
    led = fl.record_kill(led, "H1", by="grok", kind="cite", accepted=True)
    led = fl.resolve(led)
    assert fl.by_id(led)["H1"]["status"] == "killed"
    # addressing re-assert reopens it...
    led, acc, _ = fl.apply_asserts(
        led, [{"id": "H1", "claim": "x narrowed", "receipts": [{"kind": "cite", "addresses": "H1"}]}]
    )
    assert acc == ["H1"] and fl.by_id(led)["H1"]["status"] == "open"
    # ...and resolve() keeps it open — the old accepted kill is marked addressed, kept in history
    led = fl.resolve(led)
    assert fl.by_id(led)["H1"]["status"] == "open"
    assert len(fl.by_id(led)["H1"]["kills"]) == 1  # history preserved, not deleted
    # a FRESH accepted kill (unaddressed) in a later round still kills
    led = fl.record_kill(led, "H1", by="grok", kind="cite", accepted=True)
    led = fl.resolve(led)
    assert fl.by_id(led)["H1"]["status"] == "killed"


def test_reopen_resets_attempts_and_caps_cycles():
    # V2: a reopened claim must EARN `survived` with a FRESH attack (attempts reset, so it
    # can't coast to survived on its pre-kill count), and kill->reopen cycles are bounded
    # by _MAX_REOPENS.
    led = fl.new_ledger("s1")
    led, *_ = fl.apply_asserts(led, [
        {"id": "H1", "claim": "x", "receipts": [{"kind": "cite", "ok": True}]},
    ])
    reassert = {"id": "H1", "claim": "x",
                "receipts": [{"kind": "cite", "ok": True, "addresses": "H1"}]}
    # bank enough failed attacks to survive, then take an accepted kill
    for _ in range(fl.DEFAULT_K):
        led = fl.record_kill(led, "H1", by="x", kind="cite", accepted=False)
    led = fl.resolve(fl.record_kill(led, "H1", by="x", kind="cite", accepted=True))
    assert fl.by_id(led)["H1"]["status"] == "killed"
    # reopen: attempts reset, so it does NOT jump straight to `survived` on the old count
    led, acc, _ = fl.apply_asserts(led, [reassert])
    assert acc == ["H1"] and fl.by_id(led)["H1"]["attempts"] == 0
    led = fl.resolve(led)
    assert fl.by_id(led)["H1"]["status"] == "open"
    # resurrection is bounded: after _MAX_REOPENS reopens, a further re-assert is refused
    for _ in range(fl._MAX_REOPENS):
        led = fl.resolve(fl.record_kill(led, "H1", by="x", kind="cite", accepted=True))
        led, acc, rej = fl.apply_asserts(led, [reassert])
    assert acc == [] and rej  # capped — the claim stays dead
    assert fl.by_id(led)["H1"]["status"] == "killed"


def test_resolve_survives_only_with_support_and_k_failed_kills():
    led = fl.new_ledger("s1")
    led, *_ = fl.apply_asserts(led, [
        {"id": "H1", "claim": "backed", "domain": "py", "receipts": [{"kind": "cite", "ok": True}]},
    ])
    led = fl.resolve(led, k=2)
    assert fl.by_id(led)["H1"]["status"] == "open"  # supported but not yet attacked
    for _ in range(2):
        led = fl.record_kill(led, "H1", by="opus", kind="cite", accepted=False)
    led = fl.resolve(led, k=2)
    assert fl.by_id(led)["H1"]["status"] == "survived" and fl.survived_ids(led) == ["H1"]


def test_unbacked_never_survives():
    led = fl.new_ledger("s1")
    led, *_ = fl.apply_asserts(led, [{"id": "H1", "claim": "vibes", "receipts": [{"kind": "unbacked"}]}])
    for _ in range(5):
        led = fl.record_kill(led, "H1", by="x", kind="cite", accepted=False)
    led = fl.resolve(led, k=2)
    assert fl.by_id(led)["H1"]["status"] == "open"  # no support → cannot compound
    assert not fl.compoundable(fl.by_id(led)["H1"])
    assert {c["id"] for c in fl.crucible(led)} == {"H1"}


# --- reputation --------------------------------------------------------------

def test_rep_counters_and_pick_assertor():
    led = fl.new_ledger("s1")
    led = fl.bump_rep(led, "grok", "py", "kills")
    led = fl.bump_rep(led, "grok", "py", "survives")
    led = fl.bump_rep(led, "m3", "py", "deaths")
    assert fl.rep_score(led, "grok", "py") == 2
    assert fl.rep_score(led, "m3", "py") == -1
    assert fl.pick_assertor(led, ["m3", "grok"], "py") == "grok"  # higher score asserts next
    assert fl.pick_assertor(led, ["m3", "glm"], "py") == "glm"    # m3 is -1 in py, glm is 0
    assert fl.pick_assertor(led, ["m3", "glm"], "net") == "m3"    # genuine tie (0,0) → stable order


# --- compounding gate + rendering (default-deny) -----------------------------

def test_render_quarantines_unbacked_and_killed():
    led = fl.new_ledger("s1")
    led, *_ = fl.apply_asserts(led, [
        {"id": "H1", "claim": "backed one", "receipts": [{"kind": "cite", "ok": True}]},
        {"id": "H2", "claim": "vibes one", "receipts": [{"kind": "unbacked"}]},
        {"id": "H3", "claim": "dead one"},
    ])
    for _ in range(2):
        led = fl.record_kill(led, "H1", by="x", kind="cite", accepted=False)
    led = fl.record_kill(led, "H3", by="x", kind="cite", accepted=True)
    led = fl.resolve(led, k=2)
    text = fl.render_ledger(led)
    assert "SURVIVED" in text and "backed one" in text
    assert "UNBACKED" in text and "vibes one" in text  # heard, but flagged
    assert "KILLED" in text and "dead one" in text
    # the survived section must not contain the unbacked/killed claims
    survived_section = text.split("UNSETTLED")[0]
    assert "vibes one" not in survived_section and "dead one" not in survived_section


def test_summary_block_shape():
    led = fl.new_ledger("s1")
    led["round"] = 3
    led, *_ = fl.apply_asserts(led, [{"id": "H1", "claim": "x", "receipts": [{"kind": "cite", "ok": True}]}])
    for _ in range(2):
        led = fl.record_kill(led, "H1", by="x", kind="cite", accepted=False)
    led = fl.resolve(led, k=2)
    block = fl.summary_block(led)
    assert block["falsify_version"] == 1 and block["round"] == 3
    assert block["survived"] == ["H1"] and block["killed"] == [] and block["crucible"] == []


# --- fixpoint ----------------------------------------------------------------

def test_is_fixpoint_detects_no_change():
    led = fl.new_ledger("s1")
    led, *_ = fl.apply_asserts(led, [{"id": "H1", "claim": "x"}])
    assert fl.is_fixpoint(led, fl.resolve(led))  # resolve with no kills changes nothing
    moved = fl.record_kill(led, "H1", by="x", kind="cite", accepted=True)
    assert not fl.is_fixpoint(led, moved)
