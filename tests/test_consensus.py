"""PR3: council consensus signal + anonymized/Fable-last synthesis."""

from __future__ import annotations

import asyncio
import json

import pytest

import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE", "0")
    monkeypatch.setenv("ASK_FABLE_SAVE", "0")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def _run(coro):
    return asyncio.run(coro)


def _oracle(key, model, rec, prose="ans"):
    block = json.dumps({"sidecar_version": 1, "recommendation": rec,
                        "confidence": "high", "needs_context": []})
    return OracleResult("ok", key=key, text=f"{prose}\n\n```json-sidecar\n{block}\n```", model=model)


def _council_with(monkeypatch, answers):
    seen = {}

    async def fake_oracle_run(key, question, context=""):
        return answers[key]

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        seen["synth"] = question  # the composed (anonymized) synthesis prompt
        return OracleResult("ok", text="MERGED")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    out = _run(server._handle_council({"question": "q", "models": list(answers)}))
    return out, seen


# --- the consensus helper ---------------------------------------------------

def test_consensus_enum():
    assert server._consensus(["apply", "apply"]) == ("strong", False)
    assert server._consensus(["apply", "reject"]) == ("divergent", True)
    # widened: "apply" (act) vs "investigate" (oppose/gate) is material
    assert server._consensus(["apply", "investigate"]) == ("divergent", True)
    # needs_more_context is an abstention about inputs, NOT opposition → not material
    assert server._consensus(["apply", "needs_more_context"]) == ("partial", False)
    # same-polarity blockers disagree on flavour, not on whether to act → partial
    assert server._consensus(["investigate", "reject"]) == ("partial", False)
    assert server._consensus(["apply"]) == ("unknown", False)
    assert server._consensus([]) == ("unknown", False)


def test_consensus_is_coverage_aware():
    # unanimous, but only 2 of a 3-oracle panel actually voted (a broken sidecar
    # dropped the third) → NOT 'strong'. This was the worst false-confidence mode.
    assert server._consensus(["apply", "apply"], panel_size=3) == ("partial", False)
    assert server._consensus(["apply", "apply"], panel_size=2) == ("strong", False)
    # ≥2 answered but a dropped sidecar left <2 usable votes → 'partial', not 'unknown'
    assert server._consensus(["apply"], panel_size=2) == ("partial", False)
    assert server._consensus([], panel_size=2) == ("unknown", False)  # nobody voted
    assert server._consensus(["apply"], panel_size=1) == ("unknown", False)  # thin panel


def test_consensus_downgrades_all_low_confidence():
    # unanimous + full coverage but every voter hedged 'low' → 'partial', not 'strong'
    assert server._consensus(["apply", "apply"], 2, ["low", "low"]) == ("partial", False)
    assert server._consensus(["apply", "apply"], 2, ["low", "high"]) == ("strong", False)
    # a MISSING confidence is not evidence of low confidence → stays 'strong'
    assert server._consensus(["apply", "apply"], 2, ["low", None]) == ("strong", False)


def test_vote_vector_counts_missing_voters():
    assert server._vote_vector(["apply", "apply"], 2) == {"apply": 2}
    assert server._vote_vector(["apply", "reject"], 2) == {"apply": 1, "reject": 1}
    assert server._vote_vector(["apply", "apply"], 5) == {"apply": 2, "no_sidecar": 3}


# --- council wiring ---------------------------------------------------------

def test_divergent_council_surfaces_signal_and_forces_adjudication(monkeypatch):
    out, seen = _council_with(monkeypatch, {
        "fable": _oracle("fable", "claude-fable-5", "reject", "fable body"),
        "minimax": _oracle("minimax", "MiniMax-M3", "apply", "mmx body"),
    })
    assert out["consensus"] == "divergent" and out["material_disagreement"] is True
    # per-panelist recommendations surface as attribution
    assert out["sources"]["fable"]["recommendation"] == "reject"
    assert out["sources"]["minimax"]["recommendation"] == "apply"
    # the divergence note is injected so the synthesizer decides rather than averages
    assert "Do NOT average" in seen["synth"]
    # next-action must NOT claim "safe to act" when the panel opposed itself
    nxt = out["recommended_next_action"]
    assert "do not treat this as consensus" in nxt
    assert "safe to act" not in nxt


def test_partial_council_next_action_not_safe(monkeypatch):
    out, _ = _council_with(monkeypatch, {
        "fable": _oracle("fable", "claude-fable-5", "investigate"),
        "minimax": _oracle("minimax", "MiniMax-M3", "reject"),
    })
    assert out["consensus"] == "partial"
    nxt = out["recommended_next_action"]
    assert "partial" in nxt
    assert "safe to act" not in nxt


def test_synthesis_is_anonymized_with_fable_last(monkeypatch):
    _, seen = _council_with(monkeypatch, {
        "fable": _oracle("fable", "claude-fable-5", "apply", "fable body"),
        "minimax": _oracle("minimax", "MiniMax-M3", "apply", "mmx body"),
    })
    s = seen["synth"]
    assert "EXPERT A" in s and "EXPERT B" in s  # anonymized labels
    assert "claude-fable-5" not in s and "MiniMax-M3" not in s  # no model names leak
    # Fable is pinned last: the non-Fable panelist's body precedes Fable's
    assert s.index("mmx body") < s.index("fable body")


def test_strong_consensus(monkeypatch):
    out, _ = _council_with(monkeypatch, {
        "fable": _oracle("fable", "claude-fable-5", "apply"),
        "minimax": _oracle("minimax", "MiniMax-M3", "apply"),
    })
    assert out["consensus"] == "strong" and out["material_disagreement"] is False
    assert out["consensus_votes"] == {"apply": 2}  # vote vector surfaces on the payload
    assert "safe to act" in out["recommended_next_action"]


def test_apply_vs_investigate_now_material(monkeypatch):
    # widened material: "ship it" vs "look deeper first" forces adjudication.
    out, seen = _council_with(monkeypatch, {
        "fable": _oracle("fable", "claude-fable-5", "apply"),
        "minimax": _oracle("minimax", "MiniMax-M3", "investigate"),
    })
    assert out["consensus"] == "divergent" and out["material_disagreement"] is True
    assert "Do NOT average" in seen["synth"]


def test_partial_consensus_not_material(monkeypatch):
    # both block/defer (investigate vs reject): disagree on flavour, not on whether to
    # act, so it's 'partial' with no forced adjudication.
    out, seen = _council_with(monkeypatch, {
        "fable": _oracle("fable", "claude-fable-5", "investigate"),
        "minimax": _oracle("minimax", "MiniMax-M3", "reject"),
    })
    assert out["consensus"] == "partial" and out["material_disagreement"] is False
    assert "Do NOT average" not in seen["synth"]  # no forced-adjudication note


def test_broken_sidecar_voter_surfaces_in_votes(monkeypatch):
    # An oracle answers but its sidecar is unparseable → it drops from the vote and
    # shows up as no_sidecar, so a lone real vote can't masquerade as agreement.
    broken = OracleResult("ok", key="minimax", text="an answer with no sidecar block",
                          model="MiniMax-M3")
    out, _ = _council_with(monkeypatch, {
        "fable": _oracle("fable", "claude-fable-5", "apply"),
        "minimax": broken,
    })
    assert out["consensus"] == "partial"  # coverage gap, not a false 'strong'
    assert out["consensus_votes"] == {"apply": 1, "no_sidecar": 1}


def test_lone_answer_consensus_unknown(monkeypatch):
    from ask_fable.minimax import OracleResult

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text=_oracle("fable", "claude-fable-5", "apply").text)

    async def fake_mmx(question, context="", **kw):
        return OracleResult("error", kind="binary_missing", text="no mmx", model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_mmx)
    out = _run(server._handle_council({"question": "q"}))
    assert out["consensus"] == "unknown" and out["material_disagreement"] is False
