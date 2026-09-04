"""Breaker transitions are reported: one stderr line, one trace event."""

from __future__ import annotations

import asyncio
import json

import ask_fable.health as health
import ask_fable.minimax as minimax
import ask_fable.oracles as oracles
from ask_fable import trace_runtime
from ask_fable.oracle_common import OracleResult


def _trip_minimax(monkeypatch, tmp_path, *, calls: int = 5):
    monkeypatch.setenv("ASK_FABLE_BREAKER_WINDOW", "5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_THRESHOLD", "0.5")
    monkeypatch.setenv("ASK_FABLE_BREAKER_COOLDOWN", "300")
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.delenv("ASK_FABLE_QUIET", raising=False)

    async def failing(question, context="", **kw):
        return OracleResult("error", kind="timeout", text="took too long", model="MiniMax-M3")

    monkeypatch.setattr(minimax, "run", failing)

    async def go():
        with trace_runtime.tool_trace("ask_m3", {"question": "q"}) as trace:
            for _ in range(calls):
                await oracles.run("minimax", "q", "")
            trace.complete({"status": "error"})

    asyncio.run(go())
    return [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]


def test_trip_emits_stderr_line_and_trace_event(monkeypatch, tmp_path, capsys):
    events = _trip_minimax(monkeypatch, tmp_path)
    assert health.breaker.should_skip("minimax") is True
    (opened,) = [e for e in events if e["event_name"] == "breaker.opened"]
    who = oracles.label("minimax")
    assert opened["kind"] == "server" and opened["status"] == "opened"
    assert opened["provider"] == {"oracle_key": "minimax", "actual_model": who}
    assert opened["orchestration"]["breaker"] == {
        "error_rate": 1.0,
        "samples": 5,
        "window": 5,
        "cooldown_s": 300.0,
    }
    err = capsys.readouterr().err
    assert f"circuit breaker opened for {who}: 100% errors over 5/5 calls" in err
    assert "skipping it for 300s" in err


def test_shed_call_lands_as_circuit_open_provider_event(monkeypatch, tmp_path):
    """The call after the trip never touches the backend; it must still leave a
    provider.completed event, tagged circuit_open so stats keeps it out of the
    error count."""
    events = _trip_minimax(monkeypatch, tmp_path, calls=6)
    completed = [e for e in events if e["event_name"] == "provider.completed"]
    assert len(completed) == 6
    assert [e["error"]["kind"] for e in completed] == ["timeout"] * 5 + ["circuit_open"]
    assert sum(1 for e in events if e["event_name"].startswith("breaker.")) == 1


def test_disabled_breaker_reports_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ASK_FABLE_CIRCUIT_BREAKER", "0")
    events = _trip_minimax(monkeypatch, tmp_path)
    assert not [e for e in events if e["event_name"].startswith("breaker.")]
    assert "circuit breaker" not in capsys.readouterr().err


def test_shed_call_is_not_labelled_as_a_backend_call(monkeypatch, tmp_path):
    """A circuit_open skip returns before any bridge is touched, so it must not
    report transport="bridge" — otherwise nothing downstream can tell a shed call
    from a real one except the error kind."""
    events = _trip_minimax(monkeypatch, tmp_path, calls=6)
    completed = [e for e in events if e["event_name"] == "provider.completed"]
    assert completed[-1]["error"]["kind"] == "circuit_open"
    assert completed[-1]["provider"]["transport"] == "skipped"
    assert all(e["provider"]["transport"] == "bridge" for e in completed[:-1])


def test_breaker_event_is_a_child_span_of_the_tool(monkeypatch, tmp_path):
    """Nested events mint their own span parented to the tool span, as provider
    and stage spans do — reusing the tool's span id would mean three event names
    share one span."""
    events = _trip_minimax(monkeypatch, tmp_path)
    (opened,) = [e for e in events if e["event_name"] == "breaker.opened"]
    (started,) = [e for e in events if e["event_name"] == "tool.started"]
    assert opened["parent_span_id"] == started["span_id"]
    assert opened["span_id"] != started["span_id"]


def test_bookkeeping_failure_is_not_reported_as_a_failed_call(monkeypatch, tmp_path):
    """The capture is around the backend call only. A breaker/cache write that
    blows up after a successful answer must not be logged as a failed attempt for
    an oracle that was working fine."""
    import ask_fable.health as health

    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")

    async def good(question, context="", **kw):
        return OracleResult("ok", text="answered fine", model="MiniMax-M3")

    def boom(*a, **k):
        raise RuntimeError("breaker bookkeeping exploded")

    monkeypatch.setattr(minimax, "run", good)
    monkeypatch.setattr(health.breaker, "record", boom)

    async def go():
        with trace_runtime.tool_trace("ask_m3", {"question": "q"}) as trace:
            result = await oracles.run("minimax", "q", "")
            trace.complete({"status": result.status})
        return result

    result = asyncio.run(go())
    # The answer the backend produced survives a broken bookkeeping step...
    assert result.status == "ok" and result.text == "answered fine"
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    completed = [e for e in events if e["event_name"] == "provider.completed"]
    # ...and is not slandered as a failed call.
    assert [e["status"] for e in completed] == ["ok"]
    assert all((e.get("error") or {}).get("kind") != "sdk_error" for e in completed)
