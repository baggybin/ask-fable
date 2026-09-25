"""ask_websearch — the opt-in web-search / OSINT research router.

Covers: the grok search path (argv flips search ON), the Claude search path
(WebSearch/WebFetch allowed, max_turns raised, research prompt), the gemini/agy
search path (research prompt folded into -p; agy has no tool gate to flip), model
selection/validation, the disabled-by-default gate, and results being uncached.
The `grok`, `agy` CLIs and the Claude SDK are all mocked — no real model is called.
"""

from __future__ import annotations

import asyncio

from conftest import FakePopen as _FakePopen

import ask_fable.fable as fable
import ask_fable.gemini as gemini
import ask_fable.grok as grok
import ask_fable.oracles as oracles
import ask_fable.prompts as prompts
import ask_fable.server as server
import ask_fable.websearch as websearch
from ask_fable import cli_gate
from ask_fable.oracle_common import OracleResult


def _run(coro):
    return asyncio.run(coro)


# ── resolve() / websearch_model() ───────────────────────────────────────────


def test_resolve_default_is_grok(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_WEBSEARCH_MODEL", raising=False)
    assert websearch.resolve(None) == "grok"
    assert websearch.websearch_model() == "grok"


def test_resolve_aliases_and_unknown():
    assert websearch.resolve("opus") == "opus5"
    assert websearch.resolve("claude-opus-4-8") == "opus48"
    assert websearch.resolve("claude-sonnet-5") == "sonnet"
    assert websearch.resolve("fable51") == "fable"
    assert websearch.resolve("GROK") == "grok"
    assert websearch.resolve("gemini") == "gemini"
    assert websearch.resolve("agy") == "gemini"
    assert websearch.resolve("gemini-3.1-pro").lower() == websearch.resolve("gemini")
    assert websearch.resolve("nonsense") is None


def test_default_model_config_over_env(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_WEBSEARCH_MODEL", "sonnet")
    assert websearch.websearch_model() == "sonnet"
    assert websearch.resolve(None) == "sonnet"


# ── grok search path ────────────────────────────────────────────────────────


def _patch_grok(monkeypatch, seen=None, **cfg):
    monkeypatch.setattr(grok.shutil, "which", lambda b: "/usr/bin/grok")
    fake = _FakePopen(**cfg)

    def _factory(argv, **kwargs):
        if seen is not None:
            seen["argv"] = argv
        return fake

    monkeypatch.setattr(cli_gate.subprocess, "Popen", _factory)
    return fake


def test_grok_search_flips_web_search_on(monkeypatch):
    seen = {}
    _patch_grok(monkeypatch, seen, stdout="Findings...\nSources: https://x")
    res = _run(
        grok.run(
            "Who released what this week?",
            web_search=True,
            system_prompt=prompts.WEBSEARCH_SYSTEM_PROMPT,
            timeout=60,
        )
    )
    assert res.status == "ok"
    argv = seen["argv"]
    # search is ON: the hermetic --disable-web-search flag is dropped ...
    assert "--disable-web-search" not in argv
    # ... and WebSearch/WebFetch are no longer in the disallowed list.
    disallowed = argv[argv.index("--disallowed-tools") + 1]
    assert "WebSearch" not in disallowed and "WebFetch" not in disallowed
    assert "Bash" in disallowed  # least privilege: everything else still blocked
    # the research prompt rides the system override, and the budget is raised
    assert argv[argv.index("--system-prompt-override") + 1] == prompts.WEBSEARCH_SYSTEM_PROMPT
    assert int(argv[argv.index("--max-turns") + 1]) >= 6
    assert res.telemetry.tools_available is True


def test_grok_default_path_still_hermetic(monkeypatch):
    """Regression: the normal ask_grok path is unchanged — search stays OFF."""
    seen = {}
    _patch_grok(monkeypatch, seen, stdout="ok")
    _run(grok.run("q", timeout=60))
    argv = seen["argv"]
    assert "--disable-web-search" in argv
    assert "WebSearch" in argv[argv.index("--disallowed-tools") + 1]


# ── gemini / agy search path ────────────────────────────────────────────────


def _patch_agy(monkeypatch, seen=None, **cfg):
    monkeypatch.setattr(gemini.shutil, "which", lambda b: "/usr/bin/agy")
    fake = _FakePopen(**cfg)

    def _factory(argv, **kwargs):
        if seen is not None:
            seen["argv"] = argv
        return fake

    monkeypatch.setattr(cli_gate.subprocess, "Popen", _factory)
    return fake


def test_gemini_search_folds_research_prompt_into_p(monkeypatch):
    seen = {}
    _patch_agy(monkeypatch, seen, stdout="Findings...\nSources: https://x")
    res = _run(websearch.run("Who released what this week?", model="gemini", timeout=60))
    assert res.status == "ok"
    # agy has no --system channel and no allow/deny flags: the research prompt is
    # the whole switch, folded into the single -p argument.
    prompt = seen["argv"][seen["argv"].index("-p") + 1]
    assert prompt.startswith(prompts.WEBSEARCH_SYSTEM_PROMPT_AGY)
    assert "Who released what this week?" in prompt
    assert prompts.ORACLE_SYSTEM_PROMPT not in prompt
    # the agy variant rewrites the base prompt's "you have page fetch" promise
    # (which the model would act on, get denied, and lose the turn to) ...
    assert "live web search (and\npage fetch)" not in prompt
    assert "`search_web` ONLY — no page fetch" in prompt
    # ... and adds the headless tool constraint
    assert prompts.AGY_SEARCH_ONLY_NOTE in prompt
    assert "search_web — ALLOWED." in prompt
    assert res.telemetry.tools_available is True


def test_agy_prompt_variant_anchor_is_asserted():
    """The variant is built by rewriting a clause of the base prompt; if that
    clause is ever reworded the assert in prompts.py must fire rather than the
    agy prompt silently going back to promising page fetch."""
    assert prompts.WEBSEARCH_SYSTEM_PROMPT_AGY.startswith(prompts.WEBSEARCH_SYSTEM_PROMPT[:60])
    assert prompts.WEBSEARCH_SYSTEM_PROMPT_AGY != prompts.WEBSEARCH_SYSTEM_PROMPT
    assert prompts.WEBSEARCH_SYSTEM_PROMPT_AGY.endswith(prompts.AGY_SEARCH_ONLY_NOTE)


def test_gemini_default_path_is_pure_reasoning_prompt(monkeypatch):
    """Regression: plain ask_gemini still gets the oracle scope prompt, not research."""
    seen = {}
    _patch_agy(monkeypatch, seen, stdout="ok")
    res = _run(gemini.run("q", timeout=60))
    assert res.status == "ok"
    prompt = seen["argv"][seen["argv"].index("-p") + 1]
    assert prompt.startswith(prompts.ORACLE_SYSTEM_PROMPT)
    assert prompts.WEBSEARCH_SYSTEM_PROMPT not in prompt


def test_gemini_alias_routes_to_agy(monkeypatch):
    seen = {}
    _patch_agy(monkeypatch, seen, stdout="ok")
    _run(websearch.run("q", model="agy", timeout=60))
    assert seen["argv"][0] == "/usr/bin/agy"


def test_gemini_empty_stdout_surfaces_stderr(monkeypatch):
    """agy's denied-tool failure mode: empty stdout, rc 0, reason on stderr — the
    bridge must hand that reason back instead of a bare "no answer"."""
    monkeypatch.setattr(gemini.shutil, "which", lambda b: "/usr/bin/agy")
    denial = (
        'jetski: no output produced — a tool required the "read_url" permission that '
        "headless mode cannot prompt for, so it was auto-denied."
    )
    monkeypatch.setattr(
        cli_gate.subprocess, "Popen", _FakePopen.factory(stdout="", stderr=denial)
    )
    res = _run(websearch.run("q", model="gemini", timeout=60))
    assert res.status == "error" and res.kind == "sdk_error"
    assert "no answer" in res.text and "read_url" in res.text


def test_gemini_search_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(gemini.shutil, "which", lambda b: None)
    res = _run(websearch.run("q", model="gemini", timeout=60))
    assert res.status == "error" and res.kind == "binary_missing"


# ── Claude search path ──────────────────────────────────────────────────────


def test_claude_search_allows_websearch_tools(monkeypatch):
    captured = {}

    async def fake_sdk(
        message, timeout, resume, system_prompt=None, on_think=None, spec=None, web_search=False
    ):
        captured["web_search"] = web_search
        captured["system_prompt"] = system_prompt
        captured["spec"] = spec
        return OracleResult("ok", text="claude findings", model=spec.model)

    monkeypatch.setattr(fable, "_run_sdk", fake_sdk)
    res = _run(websearch.run("research this", model="sonnet"))
    assert res.status == "ok" and res.text == "claude findings"
    assert captured["web_search"] is True
    assert captured["system_prompt"] == prompts.WEBSEARCH_SYSTEM_PROMPT
    assert captured["spec"].model == "claude-sonnet-5"


def test_sdk_options_wire_tools_and_turns(monkeypatch):
    """The SDK options actually carry WebSearch/WebFetch + a raised max_turns +
    bypass permission when web_search is set."""
    import ask_fable.fable as fable_mod

    seen = {}

    class FakeOptions:
        def __init__(self, **kw):
            seen.update(kw)

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def connect(self):
            pass

        async def query(self, message):
            pass

        async def receive_response(self):
            if False:
                yield None

        async def disconnect(self):
            pass

    # Don't let option-building probe the real/faked SDK binary on disk.
    monkeypatch.setattr(fable_mod, "best_cli_path", lambda: None)

    import types as _t

    fake_sdk_mod = _t.ModuleType("claude_agent_sdk")
    fake_sdk_mod.ClaudeAgentOptions = FakeOptions
    fake_sdk_mod.ClaudeSDKClient = FakeClient
    fake_sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
    fake_sdk_mod.ResultMessage = type("ResultMessage", (), {})
    fake_sdk_mod.TextBlock = type("TextBlock", (), {})
    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", fake_sdk_mod)

    _run(
        fable_mod._run_sdk(
            "msg", 60, None, prompts.WEBSEARCH_SYSTEM_PROMPT, None, fable.fable_spec(), True
        )
    )
    assert seen["allowed_tools"] == ["WebSearch", "WebFetch"]
    assert seen["permission_mode"] == "bypassPermissions"
    assert seen["max_turns"] >= 2


# ── handler: gate, validation, no-cache ─────────────────────────────────────


def _quiet(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.guard, "check", lambda q, c="", *, trusted=False: (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


def test_handler_disabled_by_default(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.delenv("ASK_FABLE_ALLOW_WEBSEARCH", raising=False)
    out = _run(server._handle_websearch({"question": "latest news on X"}))
    assert out["status"] == "disabled"
    assert "ASK_FABLE_ALLOW_WEBSEARCH" in out["reason"]


def test_handler_bad_model(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_ALLOW_WEBSEARCH", "1")
    out = _run(server._handle_websearch({"question": "q", "model": "gpt-9"}))
    assert out["status"] == "error" and out["kind"] == "bad_args"


def test_handler_runs_and_does_not_cache(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_ALLOW_WEBSEARCH", "1")
    calls = {"n": 0}

    async def fake_run(key, question, context="", *, effort=None, model=None):
        calls["n"] += 1
        assert key == "websearch" and model == "grok"
        return OracleResult("ok", key="websearch", text="fresh findings", model="grok-4.6")

    # a cache.put would raise if the handler tried to pin an uncached tool
    def _boom_put(*a, **k):
        raise AssertionError("ask_websearch must not write the tool cache")

    monkeypatch.setattr(server.oracles, "run", fake_run)
    monkeypatch.setattr(server.cache, "put", _boom_put)
    out = _run(server._handle_websearch({"question": "who won?", "model": "grok"}))
    assert out["status"] == "ok" and out["answer"] == "fresh findings"
    assert out["model"] == "grok-4.6"
    assert calls["n"] == 1


# ── server wiring ───────────────────────────────────────────────────────────


def test_schema_annotation_and_dispatch_wired():
    assert "ask_websearch" in server._TOOL_SCHEMAS
    assert "ask_websearch" in server._TOOL_ANNOTATIONS
    # openWorld model-call profile (reaches the web, spends a turn, not read-only)
    ann = server._TOOL_ANNOTATIONS["ask_websearch"]
    assert ann.readOnlyHint is False and ann.openWorldHint is True
    # not a council seat
    assert "websearch" not in oracles.KNOWN
    enum = server._WEBSEARCH_SCHEMA["properties"]["model"]["enum"]
    assert enum == ["grok", "gemini", "sonnet", "opus48", "opus5", "fable"]
    # the schema enum is the router's own allow-list, in order — one source of truth
    assert enum == list(websearch._ALLOWED)
    # trusted is advertised so a research task with security vocabulary has the
    # operator-authorized escape hatch (it routes through _handle_single's guard).
    assert server._WEBSEARCH_SCHEMA["properties"]["trusted"]["type"] == "boolean"


def test_handler_passes_trusted_to_the_guard(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_ALLOW_WEBSEARCH", "1")
    monkeypatch.setenv("ASK_FABLE_ALLOW_TRUSTED", "1")
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    seen = {}

    def fake_check(q, c="", *, trusted=False):
        seen["trusted"] = trusted
        return True, ""

    async def fake_run(key, question, context="", *, effort=None, model=None):
        return OracleResult("ok", key="websearch", text="x", model="grok-4.6")

    monkeypatch.setattr(server.guard, "check", fake_check)
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(server.oracles, "run", fake_run)
    out = _run(
        server._handle_websearch({"question": "who won?", "model": "grok", "trusted": True})
    )
    assert out["status"] == "ok"
    assert seen["trusted"] is True
