"""debate_ledger — the json-debate block parser + deterministic resolution predicates.

Pure functions, no model calls. Mirrors test_sidecar's shape: extraction fail-safes
plus the control-flow predicates that decide conceded/converged/adjudicated/stalemate.
"""

from __future__ import annotations

from ask_fable import debate_ledger as dl


def _block(body: str, fence: str = "json-debate") -> str:
    return f"answer prose here\n\n```{fence}\n{body}\n```"


# --- extraction --------------------------------------------------------------

def test_extract_ok_strips_block():
    body = '{"debate_version":1,"role":"propose","claims":[{"id":"C1","claim":"x","load_bearing":true}]}'
    prose, led = dl.extract(_block(body))
    assert prose == "answer prose here"
    assert led["role"] == "propose" and dl.claims(led)[0]["id"] == "C1"


def test_extract_none_when_absent():
    prose, led = dl.extract("just prose, no block")
    assert prose == "just prose, no block" and led is None


def test_extract_failsafe_on_two_blocks():
    body = '{"debate_version":1,"role":"refute","dispositions":[]}'
    text = _block(body) + "\n\n" + _block(body)
    prose, led = dl.extract(text)
    # both blocks stripped (no leak), but value not trusted with >1 block
    assert "json-debate" not in prose and led is None


def test_extract_ignores_sidecar_fence():
    # a json-sidecar block is NOT a debate candidate
    text = "prose\n\n```json-sidecar\n{\"sidecar_version\":1}\n```"
    prose, led = dl.extract(text)
    assert led is None and "json-sidecar" in prose  # left for sidecar.extract to handle


def test_extract_tier2_sentinel_fallback():
    # a plain fence carrying the sentinel still counts
    text = "prose\n\n```\n{\"debate_version\":1,\"role\":\"revise\",\"resolutions\":[]}\n```"
    prose, led = dl.extract(text)
    assert led is not None and led["role"] == "revise" and prose == "prose"


def test_extract_loose_json_repair():
    # one bounded repair pass: smart quotes + trailing commas (same policy as sidecar)
    body = '{“debate_version”:1,“role”:“propose”,“claims”:[],}'
    _, led = dl.extract(_block(body))
    assert led is not None and led["role"] == "propose"


# --- accessors are defensive -------------------------------------------------

def test_accessors_tolerate_garbage():
    assert dl.claims({"claims": "nope"}) == []
    assert dl.dispositions(None) == []
    assert dl.claims({"claims": [{"id": "C1"}, "junk", 3]}) == [{"id": "C1"}]


# --- resolution predicates ---------------------------------------------------

def test_contested_after_refute_includes_added():
    led = {"dispositions": [{"id": "C1", "verdict": "contest"}, {"id": "C2", "verdict": "concede"}],
           "added_claims": [{"id": "B1", "claim": "missing"}]}
    assert dl.contested_after_refute(led) == ["C1", "B1"]


def test_contested_dedupes_and_orders():
    led = {"dispositions": [{"id": "C1", "verdict": "contest"}, {"id": "C1", "verdict": "contest"}]}
    assert dl.contested_after_refute(led) == ["C1"]


def test_severity_map_defaults_medium():
    led = {"dispositions": [{"id": "C1", "verdict": "contest", "severity": "high"},
                            {"id": "C2", "verdict": "contest"}],
           "added_claims": [{"id": "B1"}]}
    sev = dl.severity_map(led)
    assert sev == {"C1": "high", "C2": "medium", "B1": "medium"}


def test_open_after_revise_silence_stays_open():
    contested = ["C1", "C2", "C3"]
    revise = {"resolutions": [{"id": "C1", "status": "revised"}, {"id": "C2", "status": "withdrawn"}]}
    # C3 not addressed → still open; C1/C2 closed
    assert dl.open_after_revise(contested, revise) == ["C3"]


def test_open_after_revise_defended_needs_a_note():
    contested = ["C1", "C2"]
    revise = {"resolutions": [
        {"id": "C1", "status": "defended", "note": "new evidence: the lock is held at L42"},
        {"id": "C2", "status": "defended"},  # bare assertion — must stay open
    ]}
    assert dl.open_after_revise(contested, revise) == ["C2"]


def test_render_open_claims_covers_opponent_added_claims():
    propose = {"claims": [{"id": "C1", "claim": "proposer claim"}]}
    refute = {"added_claims": [{"id": "B1", "claim": "opponent-added claim"}]}
    text = dl.render_open_claims(propose, ["C1", "B1"], refute)
    assert "proposer claim" in text and "opponent-added claim" in text
    assert "unavailable" not in text


def test_render_for_adjudicator_includes_rebut_ledger():
    propose = {"claims": [{"id": "C1", "claim": "x"}]}
    refute = {"dispositions": [{"id": "C1", "verdict": "contest", "reason": "r1 reason"}]}
    rebut = {"dispositions": [{"id": "C1", "verdict": "contest", "reason": "round-2 knockout"}]}
    text = dl.render_for_adjudicator(propose, refute, None, ["C1"], rebut_l=rebut)
    assert "REBUTTAL" in text and "round-2 knockout" in text
    # Without a rebut ledger the section is absent.
    assert "REBUTTAL" not in dl.render_for_adjudicator(propose, refute, None, ["C1"])


def test_low_effort_opposition():
    # conceded with no attempted refutation → rubber stamp
    assert dl.low_effort_opposition({"dispositions": [{"id": "C1", "verdict": "concede"}]}) is True
    assert dl.low_effort_opposition(
        {"dispositions": [{"id": "C1", "verdict": "concede", "attempted_refutation": "tried X"}]}) is False
    # no concessions at all → not low effort
    assert dl.low_effort_opposition({"dispositions": [{"id": "C1", "verdict": "contest"}]}) is False


def test_is_stalemate():
    open_ids = ["C1", "C2"]
    all_restated = {"dispositions": [{"id": "C1", "verdict": "contest", "novelty": "restated"},
                                     {"id": "C2", "verdict": "contest", "novelty": "restated"}]}
    assert dl.is_stalemate(all_restated, open_ids) is True
    # a single 'new' contest breaks the stalemate
    has_new = {"dispositions": [{"id": "C1", "verdict": "contest", "novelty": "new"},
                                {"id": "C2", "verdict": "contest", "novelty": "restated"}]}
    assert dl.is_stalemate(has_new, open_ids) is False
    # nothing still contested → not a stalemate (it's convergence)
    assert dl.is_stalemate({"dispositions": [{"id": "C1", "verdict": "concede"}]}, open_ids) is False


def test_downgrade_confidence():
    assert dl.downgrade_confidence("high") == "medium"
    assert dl.downgrade_confidence("medium") == "low"
    assert dl.downgrade_confidence("low") == "low"
    assert dl.downgrade_confidence(None) is None


# --- rendering ---------------------------------------------------------------

def test_render_claims_and_dispositions():
    propose = {"claims": [{"id": "C1", "claim": "use a lock", "evidence": "line 42", "load_bearing": True}]}
    assert "[C1]" in dl.render_claims(propose) and "load-bearing" in dl.render_claims(propose)
    refute = {"dispositions": [{"id": "C1", "verdict": "contest", "reason": "deadlock",
                               "failure_scenario": "A waits B", "severity": "high"}]}
    out = dl.render_dispositions(refute)
    assert "[C1] contest" in out and "A waits B" in out and "high" in out


def test_render_empty_is_graceful():
    assert "no parseable claims" in dl.render_claims({})
    assert "no parseable dispositions" in dl.render_dispositions(None)
