"""The Fable TRANSPORT selector — where a turn is allowed to run.

Covers `ASK_FABLE_FABLE_TRANSPORT` (auto/sdk/cli/http), the `auto` attempt loop and
the narrow re-route rule, the API-key transport itself, the capability refusals, the
per-transport model ladder, and the reachability answers `oracles.available` and
`diagnose` give on a host with no Claude Code. No model is ever called — the
dispatch and the HTTP client are both stubbed.

The rules this file pins down are in the CHANGELOG entry for the selector.
"""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.diagnose as diagnose
import ask_fable.fable as fable
import ask_fable.oracles as oracles
import ask_fable.prompts as prompts
from ask_fable.oracle_common import OracleResult
from ask_fable.provider_telemetry import ProviderTelemetry


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Process-scoped state by design (the demotion set, the CLI probe) — reset it
    so one test's demotion can't decide another's outcome."""
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.delenv(fable.MODEL_ENV, raising=False)
    monkeypatch.delenv(fable.CLI_ENV, raising=False)
    monkeypatch.delenv(fable.TRANSPORT_ENV, raising=False)
    monkeypatch.delenv(fable.LEGACY_CLI_ENV, raising=False)
    monkeypatch.delenv(fable.HTTP_KEY_ENV, raising=False)
    monkeypatch.delenv(fable.HTTP_BASE_URL_ENV, raising=False)
    monkeypatch.setattr(fable, "_unavailable", set())
    fable.best_cli_path.cache_clear()
    yield
    fable.best_cli_path.cache_clear()


# --- the selector ----------------------------------------------------------


def test_auto_is_the_claude_code_ladder_only():
    """http is NOT on the auto ladder: an exported key must never turn a flat-plan
    oracle into a per-token one without the operator asking for it."""
    assert fable.transport_plan().attempts == ("sdk", "cli")


@pytest.mark.parametrize("selector", ["sdk", "cli", "http"])
def test_a_named_transport_is_a_single_attempt(selector, monkeypatch):
    """One attempt = no fallback, the same rule a pinned model already follows."""
    monkeypatch.setenv(fable.TRANSPORT_ENV, selector)
    assert fable.transport_plan().attempts == (selector,)


def test_an_unknown_selector_is_a_hard_error(monkeypatch):
    monkeypatch.setenv(fable.TRANSPORT_ENV, "htp")
    with pytest.raises(fable.TransportConfigError) as exc:
        fable.transport_plan()
    assert "htp" in str(exc.value) and "http" in str(exc.value)

    async def boom(*a, **k):  # pragma: no cover — the error must precede dispatch
        raise AssertionError("must not dispatch on a bad selector")

    monkeypatch.setattr(fable, "_dispatch", boom)
    res = _run(fable.run("How does routing work here?"))
    assert res.status == "error" and res.kind == "bad_args"
    assert fable.TRANSPORT_ENV in res.text


def test_legacy_use_cli_env_is_folded_in(monkeypatch):
    monkeypatch.setenv(fable.LEGACY_CLI_ENV, "1")
    assert fable.transport_plan().attempts == ("cli",)
    monkeypatch.setenv(fable.LEGACY_CLI_ENV, "0")
    assert fable.transport_plan().attempts == ("sdk",)


def test_a_legacy_disagreement_is_reported_not_obeyed(monkeypatch):
    monkeypatch.setenv(fable.LEGACY_CLI_ENV, "1")
    monkeypatch.setenv(fable.TRANSPORT_ENV, "http")
    plan = fable.transport_plan()
    assert plan.attempts == ("http",)
    assert plan.warning and fable.LEGACY_CLI_ENV in plan.warning


def test_use_cli_argument_pins_the_transport(monkeypatch):
    seen = []

    async def fake(transport, *a, **k):
        seen.append(transport)
        return OracleResult("ok", text="answered")

    monkeypatch.setattr(fable, "_dispatch", fake)
    monkeypatch.setenv(fable.TRANSPORT_ENV, "http")  # overridden by the call argument
    _run(fable.run("q", use_cli=True))
    assert seen == ["cli"]


# --- the attempt loop ------------------------------------------------------


def _boom_spawn(*a, **k):  # pragma: no cover — asserted by never being called
    raise AssertionError("the CLI must not be spawned")


def test_auto_steps_past_an_absent_sdk(monkeypatch):
    async def no_sdk(*a, **k):
        raise ImportError("No module named 'claude_agent_sdk'")

    async def cli(*a, **k):
        return OracleResult("ok", text="answered by the CLI")

    monkeypatch.setattr(fable, "_run_sdk", no_sdk)
    monkeypatch.setattr(fable, "_run_cli", cli)
    assert _run(fable.run("q")).text == "answered by the CLI"


def test_a_pinned_sdk_does_not_spawn_the_cli(monkeypatch):
    """The old `except ImportError: return _run_cli(...)` fired regardless of what
    the operator asked for — a pinned `sdk` must stay pinned."""
    async def no_sdk(*a, **k):
        raise ImportError("No module named 'claude_agent_sdk'")

    monkeypatch.setenv(fable.TRANSPORT_ENV, "sdk")
    monkeypatch.setattr(fable, "_run_sdk", no_sdk)
    monkeypatch.setattr(fable, "_run_cli", _boom_spawn)
    res = _run(fable.run("q"))
    assert res.status == "error" and res.kind == "sdk_unavailable"
    assert fable.TRANSPORT_ENV in res.text


@pytest.mark.parametrize(
    "kind", ["auth_failed", "timeout", "rate_limit", "refusal", "model_unavailable", "sdk_error"]
)
def test_only_an_absent_transport_reroutes(kind, monkeypatch):
    """Everything that means 'a transport answered' propagates untouched — a
    re-route there would change which bill (and which oracle) the answer came from."""
    seen = []

    async def answer(transport, *a, **k):
        seen.append(transport)
        return OracleResult("error", kind=kind, text="transport said no")

    monkeypatch.setattr(fable, "_dispatch", answer)
    res = _run(fable.run("q"))
    assert res.kind == kind
    # Never the CLI rung. `model_unavailable` legitimately dispatches twice — but
    # that second call is the MODEL ladder stepping down a rung on the SAME
    # transport, not a transport re-route: no other transport may appear.
    assert set(seen) == {"sdk"}
    assert len(seen) == (2 if kind == "model_unavailable" else 1)


def test_an_absent_cli_is_reported_as_binary_missing(monkeypatch):
    async def no_sdk(*a, **k):
        raise ImportError("nope")

    async def cli(*a, **k):
        return OracleResult("error", kind="binary_missing", text="`claude` CLI not found on PATH")

    monkeypatch.setattr(fable, "_run_sdk", no_sdk)
    monkeypatch.setattr(fable, "_run_cli", cli)
    res = _run(fable.run("q"))
    assert res.kind == "binary_missing"


# --- the model ladder is per transport -------------------------------------


def test_a_demotion_is_scoped_to_the_transport_that_rejected_it(monkeypatch):
    """A Messages-API rejection says nothing about what the OAuth ladder serves —
    a shared set would poison that id everywhere for the rest of the process."""
    fable._demote(fable.FABLE_CANDIDATES, "http", fable.FABLE_PREFERRED_MODEL)
    assert fable.fable_model("sdk") == fable.FABLE_PREFERRED_MODEL
    assert fable.fable_model("http") == fable.FABLE_MODEL
    assert fable.fable_model() == fable.FABLE_MODEL  # the display view stays honest


# --- the http transport ----------------------------------------------------


def _stub_http(monkeypatch, result=None, seen=None):
    async def fake(cfg, question, context="", *, timeout=None, system_prompt=None,
                   web_search=False):
        if seen is not None:
            seen.update(
                cfg=cfg, question=question, context=context,
                timeout=timeout, system_prompt=system_prompt, web_search=web_search,
            )
        return result if result is not None else OracleResult(
            "ok", text="answered over HTTP", model=cfg.model,
            telemetry=ProviderTelemetry(
                oracle_key=cfg.key, requested_model=cfg.model, actual_model=cfg.model,
                transport="anthropic-http",
            ),
        )

    monkeypatch.setattr(fable.anthropic_http, "run", fake)


def test_http_carries_the_spec_the_prompt_and_the_billed_basis(monkeypatch):
    seen: dict = {}
    monkeypatch.setenv(fable.TRANSPORT_ENV, "http")
    monkeypatch.setenv(fable.HTTP_KEY_ENV, "sk-ant-test")
    monkeypatch.setenv(fable.HTTP_BASE_URL_ENV, "https://api.example.test")
    _stub_http(monkeypatch, seen=seen)
    res = _run(fable.run("Where is the router defined?", "def build(): ..."))

    assert res.status == "ok"
    cfg = seen["cfg"]
    assert (cfg.key, cfg.model) == ("fable", fable.fable_model("http"))
    assert cfg.api_key == "sk-ant-test" and cfg.base_url == "https://api.example.test"
    # the caller's question/context travel un-flattened (the client composes once)
    assert seen["question"] == "Where is the router defined?"
    assert seen["context"] == "def build(): ..."
    assert seen["system_prompt"] == prompts.FABLE_SYSTEM_PROMPT
    assert res.telemetry.transport == "anthropic-http"
    # real per-token spend must never read as the flat OAuth plan
    assert res.meta["cost_basis"] == "billed"
    assert res.telemetry.usage is None


def test_a_custom_system_prompt_reaches_the_http_transport(monkeypatch):
    seen: dict = {}
    monkeypatch.setenv(fable.TRANSPORT_ENV, "http")
    monkeypatch.setenv(fable.HTTP_KEY_ENV, "k")
    _stub_http(monkeypatch, seen=seen)
    _run(fable.run("q", system_prompt=prompts.SYNTH_SYSTEM_PROMPT))
    assert seen["system_prompt"] == prompts.SYNTH_SYSTEM_PROMPT


def test_http_without_a_key_is_an_error_not_a_fallthrough(monkeypatch):
    monkeypatch.setenv(fable.TRANSPORT_ENV, "http")
    monkeypatch.setattr(fable, "_run_sdk", _boom_spawn)
    monkeypatch.setattr(fable, "_run_cli", _boom_spawn)
    res = _run(fable.run("q"))
    assert res.status == "error" and res.kind == "not_configured"
    assert fable.HTTP_KEY_ENV in res.text


def test_http_refuses_a_resume_it_cannot_do(monkeypatch):
    """No server-side session to continue over HTTP — a caller-visible refusal,
    never a silently fresh thread answering as if it remembered."""
    monkeypatch.setenv(fable.TRANSPORT_ENV, "http")
    monkeypatch.setenv(fable.HTTP_KEY_ENV, "k")
    res = _run(fable.run("q", resume="sid-from-a-sdk-thread"))
    assert res.status == "error" and res.kind == "transport_incapable"
    assert res.kind not in fable.REROUTE_KINDS


def test_an_http_turn_hands_back_a_marker_its_follow_up_is_refused_on(monkeypatch):
    """The guard above never fired in practice: http returned no session id, so `ask`
    stored nothing and the follow-up arrived WITHOUT a resume — answered fresh and
    memoryless, reported ok. A marker id makes the follow-up carry one to refuse."""
    seen: dict = {}
    monkeypatch.setenv(fable.TRANSPORT_ENV, "http")
    monkeypatch.setenv(fable.HTTP_KEY_ENV, "k")
    _stub_http(monkeypatch, seen=seen)
    first = _run(fable.run("Remember: the retry cap is 7."))
    assert first.status == "ok" and first.session_id.startswith(fable.HTTP_SESSION_PREFIX)
    seen.clear()
    follow = _run(fable.run("What retry cap did I give?", resume=first.session_id))
    assert follow.status == "error" and follow.kind == "transport_incapable"
    assert seen == {}  # refused before any billed call
    # No Claude Code transport holds that conversation either — refused, never spawned.
    monkeypatch.setattr(fable, "_run_sdk", _boom_spawn)
    monkeypatch.setattr(fable, "_run_cli", _boom_spawn)
    for selector in ("auto", "sdk", "cli"):
        monkeypatch.setenv(fable.TRANSPORT_ENV, selector)
        res = _run(fable.run("What retry cap did I give?", resume=first.session_id))
        assert res.kind == "transport_incapable" and "reset" in res.text


def test_http_web_search_is_supported_and_forwarded(monkeypatch):
    """Phase 2: the API's own server-side search tool means ask_websearch works on a
    host with no Claude Code, so web_search rides through instead of being refused."""
    seen: dict = {}
    monkeypatch.setenv(fable.TRANSPORT_ENV, "http")
    monkeypatch.setenv(fable.HTTP_KEY_ENV, "k")
    _stub_http(monkeypatch, seen=seen)
    res = _run(fable.run("q", web_search=True))
    assert res.status == "ok"
    assert seen["web_search"] is True


def test_reroute_kinds_are_deliberately_narrow():
    assert fable.REROUTE_KINDS == frozenset({"binary_missing", "sdk_unavailable"})
    for excluded in ("auth_failed", "timeout", "rate_limit", "model_unavailable",
                     "transport_incapable", "sdk_error"):
        assert excluded not in fable.REROUTE_KINDS


# --- reachability: available() and the doctor ------------------------------


def test_available_follows_the_transport(monkeypatch):
    monkeypatch.setattr(fable, "claude_code_present", lambda: False)
    monkeh = monkeypatch
    monkeh.delenv(fable.HTTP_KEY_ENV, raising=False)
    # no Claude Code and no pinned API transport → the oracle is genuinely dark
    assert oracles.available("fable") is False
    monkeh.setenv(fable.TRANSPORT_ENV, "http")
    assert oracles.available("fable") is False  # pinned but unconfigured
    monkeh.setenv(fable.HTTP_KEY_ENV, "k")
    assert oracles.available("fable") is True


def test_available_ignores_a_bad_selector(monkeypatch):
    """A health probe must not raise on the typo it exists to help diagnose."""
    monkeypatch.setattr(fable, "claude_code_present", lambda: True)
    monkeypatch.setenv(fable.TRANSPORT_ENV, "nonsense")
    assert oracles.available("fable") is True
    assert fable.http_transport_selected() is False


def test_doctor_reports_a_missing_claude_code(monkeypatch):
    monkeypatch.setattr(fable, "claude_code_present", lambda: False)
    monkeypatch.setattr(oracles, "available", lambda key: False)
    row = _run(diagnose._probe("fable"))
    assert row["status"] == "not_configured"
    assert row["fix"] and fable.HTTP_KEY_ENV in row["fix"]
    assert any(c["name"] == "claude_code" and c["ok"] is False for c in row["checks"])


def test_doctor_accepts_the_api_transport(monkeypatch):
    monkeypatch.setattr(fable, "claude_code_present", lambda: False)
    monkeypatch.setenv(fable.TRANSPORT_ENV, "http")
    monkeypatch.setenv(fable.HTTP_KEY_ENV, "k")
    row = _run(diagnose._probe("fable"))
    assert row["status"] == "ok"
    assert any(c["name"] == "http" and c["ok"] for c in row["checks"])
