"""ask_fable conference handler — sequential rounds, rapporteur, degrade paths.

Drives ``_handle_conference`` with ``oracles.run`` stubbed and the guard allowed,
ASK_FABLE_QUIET=1 so the console reporter stays silent. No model is called.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import ask_fable.server as server
from ask_fable import conference, oracles
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _quiet_and_no_audit(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


def _run(coro):
    return asyncio.run(coro)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="", **kw: (True, ""))


def _stub_run(monkeypatch, mapping=None):
    """Stub oracles.run: per-key canned turns; the rapporteur call returns a map."""
    calls: list[tuple[str, str]] = []

    async def fake_run(key, question, context="", **kw):
        calls.append((key, question))
        if "MAP OF THE DISAGREEMENT" in question:  # the synthesis call
            return OracleResult("ok", key=key, text="CONVERGED: caching. CRUX: none.", model=key)
        text = (mapping or {}).get(key, f"{key} makes a point")
        return OracleResult("ok", key=key, text=text, model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    return calls


def test_runs_rounds_and_produces_map(monkeypatch):
    _allow(monkeypatch)
    calls = _stub_run(monkeypatch)
    out = _run(
        server._handle_conference(
            {"question": "cache in redis or postgres?", "models": ["fable", "minimax"], "rounds": 2}
        )
    )
    assert out["status"] == "ok"
    assert out["rounds"] == 2
    assert len(out["posts"]) == 4  # 2 models × 2 rounds
    assert out["map"] == "CONVERGED: caching. CRUX: none."
    # 4 turn calls + 1 synthesis
    assert len(calls) == 5
    # each debater's contribution is on the transcript
    joined = "\n".join(out["transcript"])
    assert "TOPIC: cache in redis or postgres?" in joined


def test_round_one_is_blind(monkeypatch):
    _allow(monkeypatch)
    calls = _stub_run(monkeypatch)
    _run(
        server._handle_conference(
            {"question": "cache or shard?", "models": ["fable", "minimax"], "rounds": 2}
        )
    )
    turns = [q for (_k, q) in calls if "MAP OF THE DISAGREEMENT" not in q]
    # round 1 (first two turns) is blind — no peer contribution visible yet
    assert "makes a point" not in turns[0]
    assert "makes a point" not in turns[1]
    assert "cache or shard?" in turns[0]  # but the topic is
    # round 2 reveals the committed round-1 positions
    assert "makes a point" in turns[2]


def test_guard_denied_calls_no_model(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="", **kw: (False, "prohibited_x"))
    calls = _stub_run(monkeypatch)
    out = _run(server._handle_conference({"question": "blocked topic here", "models": ["fable", "minimax"]}))
    assert out["status"] == "refused"
    assert out["reason"] == "prohibited_x"
    assert calls == []  # nothing dispatched


def test_needs_at_least_two_models(monkeypatch):
    _allow(monkeypatch)
    _stub_run(monkeypatch)
    out = _run(server._handle_conference({"question": "x", "models": ["fable"]}))
    assert out["status"] == "error"
    assert out["kind"] == "no_models"


def test_missing_topic_is_bad_args(monkeypatch):
    _allow(monkeypatch)
    _stub_run(monkeypatch)
    out = _run(server._handle_conference({"models": ["fable", "minimax"]}))
    assert out["status"] == "error"
    assert out["kind"] == "bad_args"


def test_rounds_are_clamped(monkeypatch):
    _allow(monkeypatch)
    _stub_run(monkeypatch)
    out = _run(
        server._handle_conference(
            {"question": "x", "models": ["fable", "minimax"], "rounds": 99}
        )
    )
    assert out["rounds"] == 10  # clamped to the max


def test_tool_is_registered_with_schema():
    # description + schema wired without instantiating the server closure
    assert isinstance(server.ASK_CONFERENCE_TOOL_DESCRIPTION, str)
    props = server._CONFERENCE_SCHEMA["properties"]
    assert {"models", "rounds", "synthesizer", "interactive"} <= set(props)


def test_default_candidates_are_reasonable():
    assert {"fable", "deepseek", "minimax"} <= set(conference.CANDIDATES)


def _fake_server(session):
    return SimpleNamespace(request_context=SimpleNamespace(session=session))


def test_elicitation_picker_returns_selection():
    class _Session:
        client_params = SimpleNamespace(
            capabilities=SimpleNamespace(elicitation=SimpleNamespace(form=SimpleNamespace()))
        )

        async def elicit_form(self, message, schema):
            self.schema = schema
            content = {"topic": "cache or shard?", "fable": True, "minimax": True, "rounds": 4}
            return SimpleNamespace(action="accept", content=content)

    session = _Session()
    out = _run(server._elicit_conference_setup(_fake_server(session), {}))
    assert out["action"] == "accept"
    assert out["topic"] == "cache or shard?"
    assert set(out["models"]) == {"fable", "minimax"}
    assert out["rounds"] == 4
    # the picker schema offers each candidate as a checkbox + a topic + rounds
    props = session.schema["properties"]
    assert "topic" in props and "rounds" in props
    assert all(brain in props for brain in conference.CANDIDATES)


def test_elicitation_falls_back_when_unsupported():
    session = SimpleNamespace(
        client_params=SimpleNamespace(capabilities=SimpleNamespace(elicitation=None))
    )
    out = _run(server._elicit_conference_setup(_fake_server(session), {}))
    assert out["action"] == "fallback"


def test_elicitation_declined_is_passed_through():
    class _Session:
        client_params = SimpleNamespace(
            capabilities=SimpleNamespace(elicitation=SimpleNamespace(form=SimpleNamespace()))
        )

        async def elicit_form(self, message, schema):
            return SimpleNamespace(action="decline", content=None)

    out = _run(server._elicit_conference_setup(_fake_server(_Session()), {}))
    assert out["action"] == "decline"
