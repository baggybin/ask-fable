"""ask_fable Grok bridge — local `grok` CLI argv, plain-text output, timeout kill.

The ``grok`` CLI is mocked via a fake ``subprocess.Popen``; no real model is called.
"""

from __future__ import annotations

import asyncio
import subprocess

import ask_fable.grok as grok
import ask_fable.oracles as oracles
import ask_fable.server as server
from ask_fable import cli_gate, isolation
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
    # grok's -p is agentic; never let it inspect the caller's repo via cwd
    assert seen["kwargs"].get("cwd") == str(isolation.oracle_cwd())


def test_run_strips_atlas_model_prefix(monkeypatch):
    seen = {}
    _patch(monkeypatch, seen, stdout="ok")
    res = _run(grok.run("q", model="xai/grok-4.5", timeout=60))
    assert res.status == "ok" and res.model == "grok-4.5"
    assert seen["argv"][seen["argv"].index("-m") + 1] == "grok-4.5"


def test_run_strips_openrouter_model_prefix(monkeypatch):
    """OpenRouter spells the vendor `x-ai/`; an openrouter:x-ai/grok-* token routed
    here used to reach the CLI as `-m x-ai/grok-4.6`, a model it does not know."""
    seen = {}
    _patch(monkeypatch, seen, stdout="ok")
    res = _run(grok.run("q", model="x-ai/grok-4.6", timeout=60))
    assert res.status == "ok" and res.model == "grok-4.6"
    assert seen["argv"][seen["argv"].index("-m") + 1] == "grok-4.6"


def test_oversized_prompt_is_refused_before_spawning(monkeypatch):
    """The prompt rides as ONE argv value; above ~128 KiB Popen fails with E2BIG,
    which surfaced as sdk_error and fed the circuit breaker."""
    import ask_fable.health as health

    monkeypatch.setattr(grok.shutil, "which", lambda b: "/usr/bin/grok")

    def no_spawn(*a, **k):
        raise AssertionError("must not spawn an oversized argv")

    monkeypatch.setattr(cli_gate.subprocess, "Popen", no_spawn)
    res = _run(grok.run("Review this module", "x" * 130_000))
    assert res.status == "error" and res.kind == "context_too_large"
    assert res.kind in health._NON_HEALTH_KINDS


def test_atlas_grok_token_too_big_for_the_cli_goes_to_the_gateway(monkeypatch):
    """Prefer-local holds only while the CLI can carry the prompt at all."""
    seen = []

    async def fake_grok(*a, **k):
        raise AssertionError("an oversized prompt must not be routed to the local CLI")

    async def fake_atlas(model, q, c="", **kw):
        seen.append(model)
        return OracleResult("ok", text="from atlas", model=model)

    monkeypatch.setattr(oracles.grok, "available", lambda: True)
    monkeypatch.setattr(oracles.grok, "run", fake_grok)
    monkeypatch.setattr(oracles.atlas, "configured", lambda: True)
    monkeypatch.setattr(oracles.atlas, "run", fake_atlas)
    r = _run(oracles.run("atlas:xai/grok-4.6", "q", "x" * 130_000))
    assert r.status == "ok" and seen == ["xai/grok-4.6"]


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


def test_handle_atlas_sends_an_oversized_grok_prompt_to_the_gateway(monkeypatch):
    # The single-model path must apply the same prefer-local-only-while-it-fits rule as
    # council tokens: a prompt past the CLI's argv limit goes to a configured gateway
    # instead of a local `context_too_large` whose advice loops back here.
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.guard, "check", lambda q, c="", *, trusted=False: (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    seen = {}

    async def fake_run(key, question, context="", *, effort=None, model=None):
        seen["key"] = key
        return OracleResult("ok", key=key, text="gateway answer", model=model or key)

    monkeypatch.setattr(server.grok, "available", lambda: True)
    monkeypatch.setattr(server.atlas, "configured", lambda: True)
    monkeypatch.setattr(server.oracles, "run", fake_run)
    big = "x" * 200_000
    out = _run(server._handle_atlas(
        {"question": "How does routing work?", "model": "xai/grok-4.5", "context": big}
    ))
    assert out["status"] == "ok" and seen["key"] == "atlas:xai/grok-4.5"
    # ...while a prompt that fits still prefers the local CLI
    out = _run(server._handle_atlas({"question": "How does routing work?", "model": "xai/grok-4.5"}))
    assert seen["key"] == "grok"


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
