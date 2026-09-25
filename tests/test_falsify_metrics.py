"""falsify_metrics — the acceptance report. Pure functions over synthetic ledgers.

Checks the QA bar operationalization: kill provenance, the grep ratio (pre-loaded vs
manufactured kills), the executor-off ablation, and the verdict.
"""

from __future__ import annotations

from ask_fable import falsify_metrics as fm


def _ledger(*, constructed: bool = False) -> dict:
    """H1 self-refuted (challenge), H2 counter-cited (contra), H3 survived; optionally a
    manufactured (metamorph) kill on H4."""
    claims = [
        {"id": "H1", "author": "m3", "domain": "api", "claim": "a", "status": "killed",
         "receipts": [], "kills": [{"by": "opus", "kind": "challenge", "accepted": True}], "attempts": 0},
        {"id": "H2", "author": "m3", "domain": "api", "claim": "b", "status": "killed",
         "receipts": [], "kills": [{"by": "opus", "kind": "contra", "accepted": True}], "attempts": 0},
        {"id": "H3", "author": "m3", "domain": "api", "claim": "c", "status": "survived",
         "receipts": [{"kind": "cite", "ok": True}],
         "kills": [{"by": "opus", "kind": "challenge", "accepted": False}] * 2, "attempts": 2},
    ]
    if constructed:
        claims.append({"id": "H4", "author": "m3", "domain": "api", "claim": "d", "status": "killed",
                       "receipts": [], "kills": [{"by": "opus", "kind": "metamorph", "accepted": True}],
                       "attempts": 0})
    return {"falsify_version": 1, "session": "s", "round": 2, "status": "active",
            "claims": claims, "edges": [],
            "rep": {"opus|api": {"kills": 2, "deaths": 0, "survives": 0}}}


def test_kill_provenance_and_grep_ratio_v1_is_retrieval_bound():
    led = _ledger()
    prov = fm.kill_provenance(led)
    assert prov == {"self_refuted": 1, "counter_cited": 1, "constructed": 0, "total": 2}
    assert fm.grep_ratio(led) == 1.0  # all pre-loaded
    assert fm.report(led, turns=4)["verdict"] == "retrieval-bound"


def test_constructed_kill_clears_the_bar():
    led = _ledger(constructed=True)
    assert fm.kill_provenance(led)["constructed"] == 1
    assert fm.grep_ratio(led) < 1.0
    assert fm.report(led, turns=4)["verdict"] == "frontier-probing"


def test_no_kills_is_no_falsification():
    led = {"falsify_version": 1, "claims": [
        {"id": "H1", "claim": "x", "status": "open", "receipts": [], "kills": []}]}
    assert fm.grep_ratio(led) is None
    assert fm.report(led, turns=2)["verdict"] == "no-falsification"


def test_spark_density_counts_committed_signal_per_turn():
    led = _ledger()  # survived 1 + killed 2 + rep moves 2 = 5
    assert fm.spark_density(led, turns=5) == 1.0
    assert fm.spark_density(led, turns=0) == 5.0  # guard against div-by-zero


def test_consensus_shadow_and_ablation_delta():
    led = _ledger()
    shadow = fm.consensus_shadow(led)
    assert set(fm.__dict__)  # sanity
    # executor off → everything "survives", nothing killed
    from ask_fable import falsify_ledger as fl
    assert fl.killed_ids(shadow) == [] and set(fl.survived_ids(shadow)) == {"H1", "H2", "H3"}
    ab = fm.ablation_delta(led)
    assert ab["prevented"] == 2 and ab["clerk_killed"] == ["H1", "H2"]
    assert set(ab["consensus_would_accept"]) == {"H1", "H2", "H3"}


def test_difficulty_spread_needs_priors():
    led = _ledger()
    assert fm.difficulty_spread(led, None) is None
    spread = fm.difficulty_spread(led, {"H1": 0.9, "H2": 0.6, "H3": 0.8})
    assert spread is not None and spread > 0  # attacks hit a range of priors
