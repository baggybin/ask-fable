"""ask_fable Grok bridge — local `grok` CLI argv, plain-text output, timeout kill.

The ``grok`` CLI is mocked via a fake ``subprocess.Popen``; no real model is called.
"""

from __future__ import annotations

import asyncio
import subprocess

import ask_fable.grok as grok
import ask_fable.oracles as oracles
import ask_fable.server as server
from ask_fable import cli_gate
from ask_fable.oracle_common import OracleResult


def _run(coro):
    return asyncio.run(coro)


from conftest import FakePopen as _FakePopen


def _patch(monkeypatch, seen=None, **cfg):
    monkeypatch.setattr(grok.shutil, "which", lambda b: "/usr/bin/grok")
    fake = _FakePopen(**cfg)

    def _factory(argv, **kwargs):
        if seen is not None:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(cli_gate.subprocess, "Popen", _factory)
    return fake


def test_timeout_default_prefers_grok_var(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_TIMEOUT", "600")
    monkeypatch.delenv("ASK_FABLE_GROK_TIMEOUT", raising=False)
    assert grok.grok_timeout_default() == 600.0
    monkeypatch.setenv("ASK_FABLE_GROK_TIMEOUT", "90")
    assert grok.grok_timeout_default() == 90.0


def test_run_ok_and_argv(monkeypatch):
    seen = {}
    _patch(monkeypatch, seen, stdout="Handlers register in build_server().\n")
    res = _run(grok.run("How are handlers registered?", "def build(): ...", timeout=120))
    assert res.status == "ok" and res.text == "Handlers register in build_server()."
    assert res.model == "grok-4.6"
    argv = seen["argv"]
    assert argv[0] == "/usr/bin/grok"
    assert argv[argv.index("-m") + 1] == "grok-4.6"
    assert argv[argv.index("--output-format") + 1] == "plain"
    # NOT 1: grok's agentic -p spends early turns on a repo-inspection plan for
    # context-flavored questions; 1 aborts as "Max turns reached" and 4 is too few
    # at the default high effort — 6 lets it finish.
    assert argv[argv.index("--max-turns") + 1] == "6"
    assert "--system-prompt-override" in argv
    assert "--no-subagents" in argv and "--disable-web-search" in argv
    assert argv[argv.index("-p") + 1].startswith("QUESTION:")
    assert "def build(): ..." in argv[argv.index("-p") + 1]
    assert seen["kwargs"].get("start_new_session") is True
    assert seen["kwargs"].get("stdin") == subprocess.DEVNULL


def test_run_strips_atlas_model_prefix(monkeypatch):
    seen = {}
    _patch(monkeypatch, seen, stdout="ok")
    res = _run(grok.run("q", model="xai/grok-4.5", timeout=60))
    assert res.status == "ok" and res.model == "grok-4.5"
    assert seen["argv"][seen["argv"].index("-m") + 1] == "grok-4.5"


def test_effort_maps_atlas_presets_to_low(monkeypatch):
    # Atlas presets (incl. the atlas→grok routing default of "deep") map to LOW:
    # grok's agentic -p spirals at high effort on context-heavy questions.
    seen = {}
    _patch(monkeypatch, seen, stdout="ok")
    for preset in ("quick", "standard", "deep"):
        _run(grok.run("q", effort=preset, timeout=60))
        assert seen["argv"][seen["argv"].index("--reasoning-effort") + 1] == "low", preset


def test_default_reasoning_is_low(monkeypatch):
    # No per-call effort (the ask_grok tool path) → the low default, not high.
    monkeypatch.delenv("ASK_FABLE_GROK_REASONING", raising=False)
    monkeypatch.delenv("ASK_FABLE_EFFORT", raising=False)
    seen = {}
    _patch(monkeypatch, seen, stdout="ok")
    _run(grok.run("q", timeout=60))
    assert seen["argv"][seen["argv"].index("--reasoning-effort") + 1] == "low"


def test_explicit_high_still_opts_in(monkeypatch):
    # A grok-native "high"/"medium" (e.g. via ASK_FABLE_GROK_REASONING) still gets high.
    seen = {}
    _patch(monkeypatch, seen, stdout="ok")
    _run(grok.run("q", effort="high", timeout=60))
    assert seen["argv"][seen["argv"].index("--reasoning-effort") + 1] == "high"


def test_run_refused(monkeypatch):
    _patch(monkeypatch, stdout="REFUSED: not about code")
    res = _run(grok.run("what is the best language overall really"))
    assert res.status == "refused" and res.text == "not about code"


def test_run_empty_output_is_error(monkeypatch):
    _patch(monkeypatch, stdout="   ")
    res = _run(grok.run("Trace dispatch.", timeout=60))
    assert res.status == "error" and res.kind == "sdk_error" and res.text == "Grok returned no answer"


def test_run_nonzero(monkeypatch):
    _patch(monkeypatch, returncode=1, stdout="", stderr="kaboom")
    res = _run(grok.run("Where is the router?", timeout=60))
    assert res.status == "error" and res.kind == "sdk_error"
    assert "kaboom" in res.text


def test_run_timeout_kills_process_group(monkeypatch):
    killed = []
    fake = _patch(monkeypatch, timeout_first=True)
    monkeypatch.setattr(cli_gate.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli_gate.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    res = _run(grok.run("Trace dispatch.", timeout=1))
    assert res.status == "error" and res.kind == "timeout"
    assert killed == [(fake.pid, cli_gate.signal.SIGKILL)]


def test_run_binary_missing(monkeypatch):
    monkeypatch.setattr(grok.shutil, "which", lambda b: None)
    spawned = {}
    monkeypatch.setattr(cli_gate.subprocess, "Popen", lambda *a, **k: spawned.__setitem__("popen", True))
    res = _run(grok.run("How does dispatch work?"))
    assert res.status == "error" and res.kind == "binary_missing"
    assert "popen" not in spawned


def test_looks_like_grok_model():
    assert grok.looks_like_grok_model("xai/grok-4.6") is True
    assert grok.looks_like_grok_model("grok-4.5") is True
    assert grok.looks_like_grok_model("openai/gpt-5.6-sol") is False
    assert grok.looks_like_grok_model("") is False


def test_oracle_available_and_label(monkeypatch):
    monkeypatch.setattr(oracles.grok, "available", lambda: True)
    assert oracles.available("grok") is True
    monkeypatch.setattr(oracles.grok, "available", lambda: False)
    assert oracles.available("grok") is False
    assert oracles.label("grok") == "grok-4.6"
    assert "grok" in oracles.KNOWN


def test_oracle_run_routes_to_grok(monkeypatch):
    async def fake(q, c="", **kw):
        return OracleResult("ok", text="grok ans", model="grok-4.5")

    monkeypatch.setattr(oracles.grok, "run", fake)
    r = _run(oracles.run("grok", "q"))
    assert r.key == "grok" and r.text == "grok ans"


def test_atlas_grok_token_prefers_local_cli(monkeypatch):
    """atlas:xai/grok-* should use the local grok bridge when the binary is available."""
    called = {}

    async def fake_grok(q, c="", **kw):
        called["kw"] = kw
        return OracleResult("ok", text="from local", model="grok-4.5")

    async def fake_atlas(*a, **k):
        raise AssertionError("atlas HTTP should not be called when local grok is available")

    monkeypatch.setattr(oracles.grok, "available", lambda: True)
    monkeypatch.setattr(oracles.grok, "run", fake_grok)
    monkeypatch.setattr(oracles.atlas, "run", fake_atlas)
    r = _run(oracles.run("atlas:xai/grok-4.5", "q about routing"))
    assert r.status == "ok" and r.text == "from local"
    assert r.key == "atlas:xai/grok-4.5"  # attribution keeps the token
    assert called["kw"].get("model") == "xai/grok-4.5"


def test_handle_atlas_redirects_grok_to_local(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.guard, "check", lambda q, c="", *, trusted=False: (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)

    async def fake_run(key, question, context="", *, effort=None, model=None):
        assert key == "grok"
        assert model == "grok-4.5"
        return OracleResult("ok", key="grok", text="local answer", model="grok-4.5")

    monkeypatch.setattr(server.grok, "available", lambda: True)
    monkeypatch.setattr(server.oracles, "run", fake_run)
    out = _run(server._handle_atlas({"question": "How does routing work?", "model": "xai/grok-4.5"}))
    assert out["status"] == "ok"
    assert out["answer"] == "local answer"
    assert out["model"] == "grok-4.5"


def test_handle_grok_missing_binary(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.guard, "check", lambda q, c="", *, trusted=False: (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)

    async def fake_run(key, question, context="", *, effort=None, model=None):
        return OracleResult(
            "error", key="grok", kind="binary_missing",
            text="`grok` CLI not found on PATH", model="grok-4.5",
        )

    monkeypatch.setattr(server.oracles, "run", fake_run)
    out = _run(server._handle_grok({"question": "How does routing work?"}))
    assert out["status"] == "error" and out["kind"] == "binary_missing"


def test_schema_and_tool_wired():
    assert "ask_grok" in server._TOOL_SCHEMAS
    assert "grok" in server._GROK_SCHEMA["properties"]["question"]["description"].lower()
    assert server._TOOL_SCHEMAS["ask_grok"] is server._GROK_SCHEMA
