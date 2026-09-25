"""ask_fable adversarial debate handler — propose/refute/revise/(rebut)/adjudicate.

Drives ``_handle_debate`` / ``_debate`` with ``oracles.run`` stubbed per role (role
is detected from the composed prompt) and ASK_FABLE_QUIET=1 so the reporter is
silent. No model is called. Focus: the DETERMINISTIC resolution branches.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _quiet_and_no_side_effects(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.outputs, "save", lambda **k: "/tmp/fake.md")
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    monkeypatch.setattr(server.cache, "get", lambda *a, **k: None)  # no stale cache hits
    monkeypatch.setattr(server.cache, "put", lambda *a, **k: None)


def _run(coro):
    return asyncio.run(coro)


def _sc(rec: str, conf: str = "high") -> str:
    return (f'\n\n```json-sidecar\n{{"sidecar_version": 1, "recommendation": "{rec}", '
            f'"confidence": "{conf}", "needs_context": []}}\n```')


def _dbg(role: str, **fields) -> str:
    return f'\n\n```json-debate\n{json.dumps({"debate_version": 1, "role": role, **fields})}\n```'


def _role_of(question: str) -> str:
    if "ADJUDICATOR" in question:
        return "adjudicate"
    if "revising under opposition" in question:
        return "revise"
    if "OPPONENT" in question:
        return "rebut" if "SECOND pass" in question else "refute"
    return "propose"


def _patch_run(monkeypatch, scripts: dict):
    """Point server.oracles.run at role-dispatched scripted OracleResults, recording
    the composed prompt each role saw."""
    seen = {}

    async def fake_run(key, question, context=""):
        role = _role_of(question)
        seen[role] = question
        return scripts[role](key)

    monkeypatch.setattr(server.oracles, "run", fake_run)
    return seen


def test_conceded_skips_revise_and_adjudication(monkeypatch):
    scripts = {
        "propose": lambda k: OracleResult("ok", model=k, text="PROPOSED" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x", "load_bearing": True}]) + _sc("apply")),
        "refute": lambda k: OracleResult("ok", model=k, text="I agree" + _dbg(
            "refute", dispositions=[{"id": "C1", "verdict": "concede",
                                     "attempted_refutation": "tried but sound"}]) + _sc("apply")),
    }
    seen = _patch_run(monkeypatch, scripts)
    out = _run(server._handle_debate({"question": "X or Y?", "proposer": "fable", "opponent": "minimax"}))
    assert out["status"] == "ok"
    assert out["debate"]["resolution"] == "conceded"
    assert out["answer"] == "PROPOSED"  # proposer's position stands
    assert out["debate"]["low_effort_opposition"] is False
    assert len(out["turns"]) == 2  # no revise, no adjudicate
    assert "adjudicate" not in seen and "revise" not in seen


def test_low_effort_opposition_flagged(monkeypatch):
    scripts = {
        "propose": lambda k: OracleResult("ok", model=k, text="P" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x"}]) + _sc("apply")),
        "refute": lambda k: OracleResult("ok", model=k, text="ok" + _dbg(
            "refute", dispositions=[{"id": "C1", "verdict": "concede"}]) + _sc("apply")),  # no attempt
    }
    _patch_run(monkeypatch, scripts)
    out = _run(server._handle_debate({"question": "X or Y?"}))
    assert out["debate"]["resolution"] == "conceded"
    assert out["debate"]["low_effort_opposition"] is True


def test_converged_when_resolved_and_recs_agree(monkeypatch):
    scripts = {
        "propose": lambda k: OracleResult("ok", model=k, text="P" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x"}]) + _sc("apply")),
        "refute": lambda k: OracleResult("ok", model=k, text="R" + _dbg(
            "refute", dispositions=[{"id": "C1", "verdict": "contest", "severity": "low",
                                     "failure_scenario": "z"}]) + _sc("apply")),
        "revise": lambda k: OracleResult("ok", model=k, text="REVISED" + _dbg(
            "revise", resolutions=[{"id": "C1", "status": "revised"}]) + _sc("apply")),
    }
    seen = _patch_run(monkeypatch, scripts)
    out = _run(server._handle_debate({"question": "X or Y?"}))
    assert out["debate"]["resolution"] == "converged"
    assert out["answer"] == "REVISED"
    assert "adjudicate" not in seen  # convergence short-circuits the judge
    assert len(out["turns"]) == 3


def test_adjudicated_when_claim_stays_open(monkeypatch):
    scripts = {
        "propose": lambda k: OracleResult("ok", model=k, text="P" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x"}]) + _sc("apply")),
        "refute": lambda k: OracleResult("ok", model=k, text="R" + _dbg(
            "refute", dispositions=[{"id": "C1", "verdict": "contest", "severity": "high",
                                     "failure_scenario": "boom"}]) + _sc("reject")),
        # revise addresses nothing → C1 stays open
        "revise": lambda k: OracleResult("ok", model=k, text="REV" + _dbg(
            "revise", resolutions=[]) + _sc("apply")),
        "adjudicate": lambda k: OracleResult("ok", model=k, text="RULING" + _dbg(
            "adjudicate", rulings=[{"id": "C1", "winner": "p1", "why": "cited code"}],
            decisive_argument="the deadlock never occurs because of the lock ordering") + _sc("apply")),
    }
    seen = _patch_run(monkeypatch, scripts)
    out = _run(server._handle_debate({"question": "X or Y?", "rounds": 1}))
    assert out["debate"]["resolution"] == "adjudicated"
    assert out["answer"] == "RULING"
    assert out["answered_by"] == "fable"  # adjudicator defaults to fable
    assert out["debate"]["decisive_argument"].startswith("the deadlock")
    assert out["debate"]["contested_claims_remaining"] == 1
    assert "adjudicate" in seen and len(out["turns"]) == 4


def test_degraded_debate_is_not_cached(monkeypatch):
    # F5: a debate whose adjudicator was unavailable (or that fell back) is degraded — don't
    # cache it for the TTL. A real ruling (adjudicated / stalemate) is still cached. The
    # autouse fixture stubs cache to no-ops, so re-point put at a spy and get at a real miss.
    puts: list = []
    monkeypatch.setattr(server.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(server.cache, "put", lambda k, payload: puts.append(payload))
    base = {
        "propose": lambda k: OracleResult("ok", model=k, text="P" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x"}]) + _sc("apply")),
        "refute": lambda k: OracleResult("ok", model=k, text="R" + _dbg(
            "refute", dispositions=[{"id": "C1", "verdict": "contest", "severity": "high",
                                     "failure_scenario": "boom"}]) + _sc("reject")),
        "revise": lambda k: OracleResult("ok", model=k, text="REV" + _dbg(
            "revise", resolutions=[]) + _sc("apply")),
    }
    # adjudicator errors -> degraded resolution -> must NOT cache
    _patch_run(monkeypatch, {**base,
                             "adjudicate": lambda k: OracleResult("error", kind="timeout",
                                                                  model=k, text="slow")})
    out = _run(server._handle_debate({"question": "X or Y?", "rounds": 1}))
    res = out["debate"]["resolution"]
    assert res.startswith("degraded") or res == "adjudicator_unavailable"
    assert puts == []  # degraded debate not cached

    # a real ruling IS cached
    _patch_run(monkeypatch, {**base,
                             "adjudicate": lambda k: OracleResult("ok", model=k, text="RULING"
                             + _dbg("adjudicate", rulings=[{"id": "C1", "winner": "p1"}])
                             + _sc("apply"))})
    out2 = _run(server._handle_debate({"question": "X or Y?", "rounds": 1}))
    assert out2["debate"]["resolution"] == "adjudicated"
    assert len(puts) == 1  # the real ruling was cached


def test_adjudicator_is_selectable(monkeypatch):
    """`adjudicator` swaps who rules — the ledger still reaches a third model."""
    keys: list[str] = []

    scripts = {
        "propose": lambda k: OracleResult("ok", model=k, text="P" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x"}]) + _sc("apply")),
        "refute": lambda k: OracleResult("ok", model=k, text="R" + _dbg(
            "refute", dispositions=[{"id": "C1", "verdict": "contest", "severity": "high",
                                     "failure_scenario": "boom"}]) + _sc("reject")),
        "revise": lambda k: OracleResult("ok", model=k, text="REV" + _dbg(
            "revise", resolutions=[]) + _sc("apply")),
        "adjudicate": lambda k: (keys.append(k), OracleResult(
            "ok", model=k, text="RULING" + _dbg(
                "adjudicate", rulings=[{"id": "C1", "winner": "p1"}]) + _sc("apply")))[1],
    }
    _patch_run(monkeypatch, scripts)
    out = _run(server._handle_debate(
        {"question": "X or Y?", "proposer": "minimax", "opponent": "glm", "adjudicator": "opus-5"}
    ))
    assert out["debate"]["resolution"] == "adjudicated"
    assert keys == ["opus5"]  # alias resolved to the Opus 5 pin, and it (not fable) ruled
    assert out["answered_by"] == "opus5"


def test_unknown_adjudicator_is_rejected(monkeypatch):
    out = _run(server._handle_debate({"question": "X or Y?", "adjudicator": "gpt-4"}))
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert "unknown adjudicator" in out["detail"]


def test_round2_stalemate_downgrades_confidence(monkeypatch):
    scripts = {
        "propose": lambda k: OracleResult("ok", model=k, text="P" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x"}]) + _sc("apply")),
        "refute": lambda k: OracleResult("ok", model=k, text="R" + _dbg(
            "refute", dispositions=[{"id": "C1", "verdict": "contest", "severity": "high",
                                     "failure_scenario": "boom"}]) + _sc("reject")),
        "revise": lambda k: OracleResult("ok", model=k, text="REV" + _dbg(
            "revise", resolutions=[]) + _sc("apply")),  # C1 stays open
        "rebut": lambda k: OracleResult("ok", model=k, text="STILL NO" + _dbg(
            "rebut", dispositions=[{"id": "C1", "verdict": "contest",
                                    "novelty": "restated"}]) + _sc("reject")),
        "adjudicate": lambda k: OracleResult("ok", model=k, text="RULE" + _dbg(
            "adjudicate", rulings=[{"id": "C1", "winner": "p2"}],
            decisive_argument="q") + _sc("apply", "high")),
    }
    _patch_run(monkeypatch, scripts)
    out = _run(server._handle_debate({"question": "X or Y?", "rounds": 2}))
    assert out["debate"]["resolution"] == "stalemate"
    assert out["debate"]["material_disagreement"] is True
    assert out["sidecar"]["confidence"] == "medium"  # high downgraded one notch on stalemate
    assert len(out["turns"]) == 5


def test_round2_rebuttal_reaches_the_adjudicator(monkeypatch):
    scripts = {
        "propose": lambda k: OracleResult("ok", model=k, text="P" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x"}]) + _sc("apply")),
        "refute": lambda k: OracleResult("ok", model=k, text="R" + _dbg(
            "refute", dispositions=[{"id": "C1", "verdict": "contest", "severity": "high",
                                     "failure_scenario": "boom"}]) + _sc("reject")),
        "revise": lambda k: OracleResult("ok", model=k, text="REV" + _dbg(
            "revise", resolutions=[]) + _sc("apply")),  # C1 stays open
        "rebut": lambda k: OracleResult("ok", model=k, text="NEW ANGLE" + _dbg(
            "rebut", dispositions=[{"id": "C1", "verdict": "contest", "novelty": "new",
                                    "reason": "round-two decisive counterexample"}]) + _sc("reject")),
        "adjudicate": lambda k: OracleResult("ok", model=k, text="RULE" + _dbg(
            "adjudicate", rulings=[{"id": "C1", "winner": "p2"}],
            decisive_argument="q") + _sc("reject")),
    }
    seen = _patch_run(monkeypatch, scripts)
    out = _run(server._handle_debate({"question": "X or Y?", "rounds": 2}))
    assert out["debate"]["resolution"] == "adjudicated"
    # The judge must see the opponent's round-2 arguments, not just round 1's.
    assert "round-two decisive counterexample" in seen["adjudicate"]
    assert "REBUTTAL" in seen["adjudicate"]


def test_degraded_when_opponent_unavailable(monkeypatch):
    scripts = {
        "propose": lambda k: OracleResult("ok", model=k, text="SOLO" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x"}]) + _sc("investigate")),
        "refute": lambda k: OracleResult("error", model=k, kind="not_configured", text="no glm key"),
    }
    _patch_run(monkeypatch, scripts)
    out = _run(server._handle_debate({"question": "X or Y?", "opponent": "glm"}))
    assert out["status"] == "ok"
    assert out["debate"]["resolution"] == "degraded_single_critic"
    assert out["answer"] == "SOLO"
    assert out["debate"]["degraded_reason"] == "not_configured"


def test_opponent_never_sees_proposer_confidence(monkeypatch):
    scripts = {
        "propose": lambda k: OracleResult("ok", model=k, text="P" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x"}]) + _sc("apply", "high")),
        "refute": lambda k: OracleResult("ok", model=k, text="R" + _dbg(
            "refute", dispositions=[{"id": "C1", "verdict": "concede",
                                     "attempted_refutation": "t"}]) + _sc("apply")),
    }
    seen = _patch_run(monkeypatch, scripts)
    _run(server._handle_debate({"question": "X or Y?"}))
    # the proposer's sidecar block (its confidence/recommendation) is stripped before
    # composing the refute prompt — the opponent argues blind to A's stated confidence
    proposal = seen["refute"].split("PROPOSAL (untrusted):")[1].split("Append EXACTLY TWO")[0]
    assert "sidecar_version" not in proposal and "json-sidecar" not in proposal
    assert "C1" in proposal  # but the claims ARE rendered for the opponent to dispose of


def test_proposer_refusal_is_surfaced(monkeypatch):
    scripts = {"propose": lambda k: OracleResult("refused", model=k, text="off scope")}
    _patch_run(monkeypatch, scripts)
    out = _run(server._handle_debate({"question": "hack a server"}))
    assert out["status"] == "refused" and out["reason"] == "off scope"


def test_unknown_model_errors(monkeypatch):
    _patch_run(monkeypatch, {})
    out = _run(server._handle_debate({"question": "X?", "opponent": "not-a-model"}))
    assert out["status"] == "error" and out["kind"] == "no_models"


def test_adjudicator_failure_is_not_labeled_adjudicated(monkeypatch):
    """T1 (bug hunt 2026-09-09): when the adjudicator fails, the debate falls back to
    the proposer's OWN revised answer — so it must NOT report resolution='adjudicated'
    (no neutral party ruled) and must downgrade that self-declared confidence."""
    scripts = {
        "propose": lambda k: OracleResult("ok", model=k, text="P" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x"}]) + _sc("apply")),
        "refute": lambda k: OracleResult("ok", model=k, text="R" + _dbg(
            "refute", dispositions=[{"id": "C1", "verdict": "contest", "severity": "high",
                                     "failure_scenario": "boom"}]) + _sc("reject")),
        "revise": lambda k: OracleResult("ok", model=k, text="REV" + _dbg(
            "revise", resolutions=[]) + _sc("apply", "high")),  # C1 stays open, high conf
        "adjudicate": lambda k: OracleResult("error", model=k, kind="timeout",
                                             text="judge timed out"),
    }
    _patch_run(monkeypatch, scripts)
    out = _run(server._handle_debate({"question": "X or Y?", "rounds": 1}))
    assert out["status"] == "ok"
    assert out["debate"]["resolution"] == "adjudicator_unavailable"
    assert out["answer"] == "REV"  # proposer's own revised answer, not a ruling
    assert out["debate"]["adjudication_failed"] == "timeout"
    assert out["sidecar"]["confidence"] == "medium"  # high downgraded — no neutral ruling
    assert out["debate"]["material_disagreement"] is True


@pytest.mark.parametrize(("rounds", "hung", "expected"), [(1, "revise", "P"), (2, "adjudicate", "REV")])
def test_timeout_falls_back_to_the_proposer_not_the_opponent(monkeypatch, rounds, hung, expected):
    """On a debate timeout the fallback is the proposer's latest position. The last ok
    turn can be the opponent's refutation (a hung revise) or its round-2 rebuttal (a
    hung adjudicator) — returning that made the attack the verdict. With no neutral
    ruling, the proposer's confidence is downgraded like any unadjudicated verdict."""
    monkeypatch.setattr(server, "_chain_timeout_s", lambda n: 0.2)
    scripts = {
        "propose": lambda k: OracleResult("ok", model=k, text="P" + _dbg(
            "propose", claims=[{"id": "C1", "claim": "x"}]) + _sc("apply", "high")),
        "refute": lambda k: OracleResult("ok", model=k, text="REFUTATION" + _dbg(
            "refute", dispositions=[{"id": "C1", "verdict": "contest", "severity": "high",
                                     "failure_scenario": "boom"}]) + _sc("reject")),
        "revise": lambda k: OracleResult("ok", model=k, text="REV" + _dbg(
            "revise", resolutions=[]) + _sc("apply", "high")),  # C1 stays open
        "rebut": lambda k: OracleResult("ok", model=k, text="REBUTTAL" + _dbg(
            "rebut", dispositions=[{"id": "C1", "verdict": "contest", "novelty": "new",
                                    "reason": "fresh counterexample"}]) + _sc("reject")),
    }

    async def fake_run(key, question, context=""):
        role = _role_of(question)
        if role == hung:
            await asyncio.sleep(5)  # past the patched debate cap
        return scripts[role](key)

    monkeypatch.setattr(server.oracles, "run", fake_run)
    out = _run(server._handle_debate({"question": "X or Y?", "rounds": rounds}))
    assert out["status"] == "ok"
    assert out["debate"]["resolution"] == "degraded_timeout"
    assert out["answer"] == expected  # the proposer's own position, never the attack
    assert out["answered_by"] == "fable"  # the proposer, not the opponent (minimax)
    assert out["sidecar"]["confidence"] == "medium"  # high downgraded — no neutral ruling
    # M8 (bug hunt 2026-09-25): the clock ran out, it did not settle the argument. C1
    # was contested and left open, so a caller keying on `material_disagreement` (as
    # the skills instruct) must not read a timeout as a clean win.
    assert out["debate"]["contested_claims_remaining"] == 1
    assert out["debate"]["material_disagreement"] is True
