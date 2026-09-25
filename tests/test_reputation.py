"""reputation — the persistent per-(model, domain) calibration store.

Uses an env-overridden temp DB so nothing touches the real store. Covers the shrunk score,
the unseen-pair prior, and feeding a resolved ask_falsify ledger (idempotent).
"""

from __future__ import annotations

import pytest

from ask_fable import falsify_ledger as fl
from ask_fable import reputation


@pytest.fixture(autouse=True)
def _temp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_REPUTATION_PATH", str(tmp_path / "rep.db"))


def test_unseen_pair_returns_prior():
    assert reputation.score("newmodel", "algo") == 0.5


def test_record_and_shrunk_score():
    for _ in range(20):
        reputation.record("m", "algo", correct=True)
    # 20/20 observed=1.0 shrunk toward 0.5 with k=10: 0.5 + 20/30*(1-0.5) = 0.833…
    assert reputation.score("m", "algo") == pytest.approx(0.5 + (20 / 30) * 0.5, abs=1e-6)
    # a different domain for the same model is independent
    assert reputation.score("m", "other") == 0.5


def test_shrinkage_favors_more_evidence():
    reputation.record("small", "d", correct=True)          # 1/1
    for _ in range(50):
        reputation.record("big", "d", correct=True)        # 50/50
    assert reputation.score("big", "d") > reputation.score("small", "d")  # more n → closer to 1.0


def test_snapshot_lists_rows():
    reputation.record("m", "algo", correct=True)
    reputation.record("m", "algo", correct=False)
    snap = {(r["model"], r["domain"]): r for r in reputation.snapshot()}
    assert snap[("m", "algo")]["n"] == 2 and snap[("m", "algo")]["wins"] == 1


def test_record_ledger_outcomes_is_idempotent():
    ledger = fl.new_ledger("s")
    ledger["claims"] = [
        {"id": "H1", "author": "mA", "domain": "algo", "status": "survived", "receipts": [], "kills": []},
        {"id": "H2", "author": "mB", "domain": "algo", "status": "killed", "receipts": [], "kills": []},
        {"id": "H3", "author": "mC", "domain": "algo", "status": "open", "receipts": [], "kills": []},
    ]
    assert reputation.record_ledger_outcomes(ledger) == 2   # survived + killed; open is skipped
    assert reputation.record_ledger_outcomes(ledger) == 0   # replay is a no-op (DB idempotency key)
    assert reputation.score("mA", "algo") > 0.5             # a win
    assert reputation.score("mB", "algo") < 0.5             # a loss
    assert reputation.score("mC", "algo") == 0.5            # never resolved → prior


def test_flipped_outcome_replaces_the_recorded_one():
    # The store kept a claim's FIRST outcome forever: a survived claim later killed by a
    # contra (or a killed one reopened that then survived) still counted as its old result.
    ledger = fl.new_ledger("s")
    ledger["claims"] = [
        {"id": "H1", "author": "mA", "domain": "algo", "claim": "c", "status": "survived",
         "receipts": [], "kills": []},
    ]
    assert reputation.record_ledger_outcomes(ledger) == 1
    ledger["claims"][0]["status"] = "killed"
    assert reputation.record_ledger_outcomes(ledger) == 1  # the flip is recorded...
    assert reputation.record_ledger_outcomes(ledger) == 0  # ...once
    row = {(r["model"], r["domain"]): r for r in reputation.snapshot()}[("mA", "algo")]
    assert row["n"] == 1 and row["wins"] == 0  # still one datapoint, now a loss
    assert reputation.score("mA", "algo") < 0.5


def test_same_id_different_text_is_not_deduped():
    # F2a: claim ids are MODEL-assigned, so two concurrent runs can mint "H1" for DIFFERENT
    # claims. The key binds the id to a hash of the claim text, so they don't collapse into one.
    assert reputation.record_outcome("s", "H1", "claim A", "mA", "algo", True) is True
    assert reputation.record_outcome("s", "H1", "claim A", "mA", "algo", True) is False  # true dup
    assert reputation.record_outcome("s", "H1", "a DIFFERENT claim", "mA", "algo", True) is True
    snap = {(r["model"], r["domain"]): r for r in reputation.snapshot()}
    assert snap[("mA", "algo")]["n"] == 2  # two distinct claims, not one
