"""Extra pinned Anthropic models (Opus 4.8, Sonnet 5) and the council
lab-diversity guard.

Three layers, mirroring test_opus.py: the pinned-spec bridges (same Claude
transport as Fable, different model id), the oracle registry (tokens usable in a
chain/council, but kept OUT of the tier presets), and the single-turn `ask_sonnet`
tool. Plus the independence gate: a unanimous council that spans only one training
lineage is downgraded from 'strong', because same-lab agreement is not independent
evidence. No model is ever called.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import ask_fable.anthropic_variants as av
import ask_fable.fable as fable
import ask_fable.oracles as oracles
import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")


# --- bridges (pinned specs) -----------------------------------------------


def test_specs_are_pinned_and_not_laddered():
    assert av.SPECS["opus48"].model == "claude-opus-4-8"
    assert av.SPECS["sonnet"].model == "claude-sonnet-5"
    # each bridge exposes the module-shaped surface the registry dispatches on
    for key, spec in av.SPECS.items():
        assert av.BRIDGES[key].MODEL == spec.model


def test_sdk_path_carries_the_pinned_spec(monkeypatch):
    seen: dict = {}

    async def fake_sdk(
        message, timeout, resume, system_prompt=None, on_think=None, spec=None, web_search=False
    ):
        seen["spec"] = spec
        return OracleResult("ok", text="ok", session_id="sid-1")

    monkeypatch.setattr(fable, "_run_sdk", fake_sdk)
    _run(av.BRIDGES["sonnet"].run("Trace the call path.", resume="sid-0"))
    assert seen["spec"] is av.SPECS["sonnet"]
    assert seen["spec"].model == "claude-sonnet-5" and seen["spec"].key == "sonnet"


# --- registry -------------------------------------------------------------


def test_registered_as_named_oracles_only():
    for key in ("opus48", "sonnet"):
        assert key in oracles.KNOWN
        # ride the OAuth session, like fable/opus — no key or CLI to configure
        assert oracles.available(key) is True
    assert oracles.label("opus48") == "claude-opus-4-8"
    assert oracles.label("sonnet") == "claude-sonnet-5"


def test_excluded_from_every_tier_preset():
    # Same-lab variants add no council diversity, so no blanket fan-out includes
    # them — they earn a seat only when a caller NAMES them.
    for tier in ("default", "middle", "full"):
        members = oracles.tier_models(tier)
        assert "opus48" not in members and "sonnet" not in members


def test_operator_aliases_resolve():
    assert oracles.resolve_ordered(["opus4.8", "opus-4.8", "claude-opus-4-8"]) == (
        ["opus48", "opus48", "opus48"],
        [],
    )
    assert oracles.resolve_ordered(["sonnet5", "SONNET-5", "claude-sonnet-5"]) == (
        ["sonnet", "sonnet", "sonnet"],
        [],
    )
    # `opus` is the ladder (newest Opus); `opus5`/`opus55` NAME a specific version
    # and are distinct pins, never folded back into `opus`.
    assert oracles.resolve_ordered(["opus", "opus5", "opus55"]) == (
        ["opus", "opus5", "opus55"],
        [],
    )
    assert oracles.resolve_ordered(["opus-5", "claude-opus-5"]) == (["opus5", "opus5"], [])
    assert oracles.resolve_ordered(["opus5.5", "opus-5.5", "claude-opus-5-5"]) == (
        ["opus55", "opus55", "opus55"],
        [],
    )


def test_run_dispatches_to_the_pinned_bridge(monkeypatch):
    seen: dict = {}

    async def fake_run(q, c="", **kw):
        seen.update(kw)
        return OracleResult("ok", text="sonnet ans")

    monkeypatch.setattr(oracles.anthropic_variants.BRIDGES["sonnet"], "run", fake_run)
    r = _run(oracles.run("sonnet", "q"))
    assert r.key == "sonnet" and r.text == "sonnet ans" and r.model == "claude-sonnet-5"
    # the system-prompt channel is used directly, as for every Claude bridge
    assert "system_prompt" in seen


def test_can_synthesize_a_council(monkeypatch):
    async def fake_run(q, c="", **kw):
        assert kw.get("system_prompt")  # SYNTH prompt rides the system channel
        return OracleResult("ok", text="merged")

    monkeypatch.setattr(oracles.anthropic_variants.BRIDGES["opus48"], "run", fake_run)
    r = _run(oracles.run_synthesis("opus48", "reconcile these"))
    assert r.status == "ok" and r.text == "merged" and r.key == "opus48"


# --- ask_sonnet tool ------------------------------------------------------


def test_tool_is_registered():
    from ask_fable import prompts

    # ask_sonnet is now a LEGACY name folded into ask_model(provider="sonnet"):
    # still callable/validated, no longer advertised.
    assert server._TOOL_SCHEMAS["ask_sonnet"] is server._SONNET_SCHEMA
    assert prompts.ASK_SONNET_TOOL_DESCRIPTION
    assert server._schema_error("ask_sonnet", {"question": "q"}) is None
    assert server._schema_error("ask_sonnet", {"question": "q", "bogus": 1}) is not None
    # single-turn: no session/reset grammar, unlike ask/ask_opus5
    assert "session" not in server._SONNET_SCHEMA["properties"]


def test_answers_and_reports_the_sonnet_model(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)

    async def fake_run(key, question, context="", **kw):
        assert key == "sonnet"
        return OracleResult("ok", key="sonnet", text="Sonnet answered.", model="claude-sonnet-5")

    monkeypatch.setattr(server.oracles, "run", fake_run)
    out = _run(server._handle_sonnet({"question": "How does routing work here?"}))
    assert out["status"] == "ok" and out["model"] == "claude-sonnet-5"
    assert out["answer"] == "Sonnet answered."


# --- lab-diversity helpers ------------------------------------------------


def test_lab_of_maps_known_and_dynamic_tokens():
    assert oracles.lab_of("fable") == "anthropic"
    assert oracles.lab_of("opus48") == "anthropic" and oracles.lab_of("sonnet") == "anthropic"
    assert oracles.lab_of("minimax") == "minimax" and oracles.lab_of("codex") == "openai"
    # dynamic gateway tokens: matched by substring against the model id
    assert oracles.lab_of("atlas:openai/gpt-5.6-sol") == "openai"
    assert oracles.lab_of("openrouter:anthropic/claude-opus-4-8") == "anthropic"
    # an unknown provider stays its OWN lab — never silently merged into another
    assert oracles.lab_of("openrouter:acme/mystery-1") == "openrouter:acme/mystery-1"


def test_distinct_labs_counts_families_not_models():
    # the whole point: four Anthropic voices are ONE independent opinion
    assert oracles.distinct_labs(["fable", "opus", "opus48", "sonnet"]) == 1
    assert oracles.distinct_labs(["fable", "minimax", "codex"]) == 3


# --- council independence gate --------------------------------------------


def _oracle(key, model, rec):
    block = json.dumps(
        {"sidecar_version": 1, "recommendation": rec, "confidence": "high", "needs_context": []}
    )
    return OracleResult("ok", key=key, text=f"ans\n\n```json-sidecar\n{block}\n```", model=model)


def _council_with(monkeypatch, answers):
    async def fake_oracle_run(key, question, context=""):
        return answers[key]

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="MERGED")

    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    return _run(server._handle_council({"question": "q", "models": list(answers)}))


def test_same_lab_unanimous_is_downgraded(monkeypatch):
    # fable + opus both say "apply" — would be 'strong', but they're one lab.
    out = _council_with(
        monkeypatch,
        {
            "fable": _oracle("fable", "claude-fable-5", "apply"),
            "opus": _oracle("opus", "claude-opus-5", "apply"),
        },
    )
    assert out["consensus"] == "partial"  # NOT 'strong' — correlated errors
    assert out["independent_labs"] == 1
    nxt = out["recommended_next_action"]
    assert "one lab" in nxt and "safe to act" not in nxt


def test_cross_lab_unanimous_stays_strong(monkeypatch):
    out = _council_with(
        monkeypatch,
        {
            "fable": _oracle("fable", "claude-fable-5", "apply"),
            "minimax": _oracle("minimax", "MiniMax-M3", "apply"),
        },
    )
    assert out["consensus"] == "strong" and out["independent_labs"] == 2
    assert "safe to act" in out["recommended_next_action"]
