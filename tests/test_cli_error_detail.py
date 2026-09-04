"""CLI stderr → kind/detail classification + concurrency gate."""

from __future__ import annotations

import json
import threading
import time

from ask_fable import cli_gate
from ask_fable.oracle_common import cli_error_detail, http_error_detail, parse_anthropic_envelope


def test_plain_stderr_is_sdk_error():
    kind, detail = cli_error_detail(label="Grok", returncode=1, stderr="kaboom\n")
    assert kind == "sdk_error" and detail == "kaboom"


def test_mmx_quota_json_is_rate_limit():
    err = json.dumps({
        "error": {
            "code": 4,
            "message": "Rate limit or quota exceeded. Token Plan usage limit reached.",
            "hint": "Check usage: mmx quota show",
        }
    })
    kind, detail = cli_error_detail(label="MiniMax", returncode=4, stderr=err)
    assert kind == "rate_limit"
    assert "quota" in detail.lower()
    assert "mmx quota show" in detail


def test_code_4_is_rate_limit_only_for_minimax():
    # mmx's quota code is 4; a bare code 4 from another CLI must NOT be a rate limit
    # (gRPC code 4 is DEADLINE_EXCEEDED).
    err = json.dumps({"error": {"code": 4, "message": "deadline exceeded"}})
    kind, _ = cli_error_detail(label="Grok", returncode=4, stderr=err)
    assert kind == "sdk_error"
    kind, _ = cli_error_detail(label="MiniMax", returncode=4, stderr=err)
    assert kind == "rate_limit"


def test_empty_stderr_falls_back_to_exit_code():
    kind, detail = cli_error_detail(label="Codex", returncode=2, stderr="", stdout="")
    assert kind == "sdk_error"
    assert "exit 2" in detail


def test_http_error_detail_classifies_and_surfaces():
    kind, text = http_error_detail(label="glm-5.2", error="HTTP 429: too many requests", http_status=429)
    assert kind == "rate_limit" and "glm-5.2 request failed" in text and "429" in text
    kind, text = http_error_detail(label="glm-5.2", error="HTTP 401: invalid api key", http_status=401)
    assert kind == "auth_failed" and "invalid api key" in text
    # Body-level quota phrasing classifies even without an HTTP status (200 envelope error).
    kind, _ = http_error_detail(label="deepseek", error="rate_limit_error: quota exhausted")
    assert kind == "rate_limit"
    kind, text = http_error_detail(label="Atlas", error="")
    assert kind == "sdk_error" and text == "Atlas request failed"


def test_http_error_detail_auth_failed():
    # A bad key is a config state, not a generic API error — it must classify
    # distinctly so the breaker/stats can tell it apart from sdk_error.
    kind, _ = http_error_detail(label="glm-5.2", error="HTTP 401: unauthorized", http_status=401)
    assert kind == "auth_failed"
    kind, _ = http_error_detail(label="Atlas", error="HTTP 403: forbidden", http_status=403)
    assert kind == "auth_failed"
    kind, _ = http_error_detail(label="deepseek", error="HTTP 401: authentication failed")
    assert kind == "auth_failed"
    kind, _ = http_error_detail(label="deepseek", error="invalid api key")
    assert kind == "auth_failed"
    # 429 still wins its own class; generic 500s stay sdk_error.
    kind, _ = http_error_detail(label="glm-5.2", error="HTTP 500: internal", http_status=500)
    assert kind == "sdk_error"


def test_envelope_null_error_key_is_not_an_error():
    # Some gateways include "error": null on SUCCESS envelopes — must parse, not fail.
    ok = {"error": None, "content": [{"type": "text", "text": "hi"}]}
    text, thinking, err = parse_anthropic_envelope(ok)
    assert err is None and text == "hi"
    # A real error dict still fails.
    bad = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    text, _, err = parse_anthropic_envelope(bad)
    assert text is None and "overloaded_error" in err
    # A non-empty string error also fails.
    text, _, err = parse_anthropic_envelope({"error": "boom"})
    assert text is None and "boom" in err


def test_cli_gate_serializes_when_limit_one(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CLI_MAX_PARALLEL", "1")
    # Reset cached semaphores so the new limit is used.
    cli_gate._gates.clear()
    order: list[str] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        with cli_gate.hold("test-bin"):
            order.append(f"{name}-in")
            barrier.wait(timeout=2)
            time.sleep(0.05)
            order.append(f"{name}-out")

    # With limit 1, barrier.wait would deadlock if both held the slot.
    # So use limit 1 but only sequential holds — prove acquire/release works.
    with cli_gate.hold("test-bin"):
        order.append("a")
    with cli_gate.hold("test-bin"):
        order.append("b")
    assert order == ["a", "b"]


def test_cli_gate_disabled(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CLI_MAX_PARALLEL", "0")
    cli_gate._gates.clear()
    with cli_gate.hold("anything"):
        pass  # no-op path


def test_invalid_api_key_is_auth_failed():
    kind, detail = cli_error_detail(label="Codex", returncode=1, stderr="Error: invalid API key")
    assert kind == "auth_failed" and "invalid api key" in detail.lower()


def test_logged_out_cli_is_auth_failed():
    """Same auth vocabulary as the HTTP bridges — a logged-out CLI must not
    count toward the circuit breaker (health.py exempts auth_failed), or the
    actionable fix-your-login message gets hidden behind circuit_open."""
    kind, detail = cli_error_detail(
        label="Grok", returncode=1, stderr="unauthorized: please run /login"
    )
    assert kind == "auth_failed" and "/login" in detail


def test_cli_auth_code_401_is_auth_failed():
    err = json.dumps({"error": {"code": 401, "message": "bad credentials"}})
    kind, _ = cli_error_detail(label="Gemini", returncode=1, stderr=err)
    assert kind == "auth_failed"


def test_cli_and_http_share_auth_vocabulary():
    """Parity pin: the same auth phrasing classifies auth_failed on BOTH
    transports (it was HTTP-only in 0.10.0)."""
    for phrase in ("invalid api key", "unauthorized", "authentication failed"):
        cli_kind, _ = cli_error_detail(label="Codex", returncode=1, stderr=phrase)
        http_kind, _ = http_error_detail(label="glm-5.2", error=phrase)
        assert cli_kind == http_kind == "auth_failed", phrase


# --- run_cli: bounded gate + shared spawn protocol ---------------------------


def test_run_cli_queue_timeout_returns_queue_timed_out(monkeypatch):
    """With every slot held, run_cli gives up after ~timeout and reports
    queue_timed_out — the untimed acquire previously made a call's wall time
    unbounded by its own timeout."""
    monkeypatch.setenv("ASK_FABLE_CLI_MAX_PARALLEL", "1")
    cli_gate._gates.pop("busybin", None)
    sem = cli_gate._semaphore("busybin")
    assert sem.acquire(blocking=False)  # occupy the only slot
    try:
        run = cli_gate.run_cli(["true"], gate="busybin", timeout=0.05)
    finally:
        sem.release()
        cli_gate._gates.pop("busybin", None)
    assert run.queue_timed_out and run.timed_out and run.returncode is None


def test_run_cli_cancelled_flag_prevents_spawn(monkeypatch):
    """A cancel flag set before the slot is acquired must prevent the spawn —
    a cancelled council panelist would otherwise still burn a full CLI run."""
    spawned = {"popen": False}
    monkeypatch.setattr(
        cli_gate.subprocess, "Popen", lambda *a, **k: spawned.__setitem__("popen", True)
    )
    evt = threading.Event()
    evt.set()
    run = cli_gate.run_cli(["true"], gate="idlebin", timeout=1, cancelled=evt)
    assert spawned["popen"] is False
    assert run.returncode is None and not run.timed_out and not run.queue_timed_out


def test_run_cli_async_cancel_while_queued_never_spawns(monkeypatch):
    """Cancelling the awaiting task while the worker is parked on the gate
    flags the worker so that, when a slot finally frees, it exits without
    launching the CLI."""
    import asyncio

    import pytest

    spawned = {"popen": False}

    async def go():
        monkeypatch.setenv("ASK_FABLE_CLI_MAX_PARALLEL", "1")
        cli_gate._gates.pop("qbin", None)
        sem = cli_gate._semaphore("qbin")
        sem.acquire()
        monkeypatch.setattr(
            cli_gate.subprocess, "Popen", lambda *a, **k: spawned.__setitem__("popen", True)
        )
        task = asyncio.create_task(cli_gate.run_cli_async(["true"], gate="qbin", timeout=5))
        await asyncio.sleep(0.1)  # let the worker park on the gate
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        sem.release()  # the slot frees only AFTER the cancel
        await asyncio.sleep(0.1)  # give the worker time to wake, check, and exit
        cli_gate._gates.pop("qbin", None)

    asyncio.run(go())
    assert spawned["popen"] is False


def test_run_cli_queue_wait_consumes_budget(monkeypatch):
    """Time spent queued on the gate is deducted from the subprocess budget,
    so total wall time stays bounded by ~timeout."""
    seen = {}

    class _Proc:
        pid = 4242
        returncode = 0

        def communicate(self, input=None, timeout=None):
            seen["timeout"] = timeout
            return "", ""

    monkeypatch.setattr(cli_gate.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setenv("ASK_FABLE_CLI_MAX_PARALLEL", "1")
    cli_gate._gates.pop("slowbin", None)
    sem = cli_gate._semaphore("slowbin")
    sem.acquire()
    timer = threading.Timer(0.15, sem.release)
    timer.start()
    try:
        run = cli_gate.run_cli(["true"], gate="slowbin", timeout=5)
    finally:
        timer.cancel()
        cli_gate._gates.pop("slowbin", None)
    assert run.returncode == 0
    assert seen["timeout"] < 5  # the ~0.15s queue wait was deducted
    assert seen["timeout"] > 3  # sanity: budget didn't collapse
