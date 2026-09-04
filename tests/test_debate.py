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
        {"question": "X or Y?", "proposer": "minimax", "opponent": "glm", "adjudicator": "opus5"}
    ))
    assert out["debate"]["resolution"] == "adjudicated"
    assert keys == ["opus"]  # alias resolved, and opus (not fable) ruled
    assert out["answered_by"] == "opus"


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
