"""The Fable model ladder: pick the best id, fall back when it is not runnable.

`ask` is no longer pinned to a model id. It asks for the newest Fable in
``FABLE_CANDIDATES``, and if the local transport rejects that id it demotes it
for the life of the process and retries one rung down. Three layers are covered
here: the ``model_unavailable`` classification the ladder triggers on, the
ladder itself (including what must NOT fall back), and the `fable51` token that
pins 5.1 explicitly. No model is ever called.
"""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.fable as fable
import ask_fable.fable51 as fable51
import ask_fable.health as health
import ask_fable.oracles as oracles
from ask_fable.oracle_common import OracleResult, cli_error_detail, http_error_detail


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """The demotion set and the CLI probe are process-scoped caches by design —
    reset both so one test's demotion can't decide another's outcome."""
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.delenv(fable.MODEL_ENV, raising=False)
    monkeypatch.delenv(fable.CLI_ENV, raising=False)
    monkeypatch.setattr(fable, "_unavailable", set())
    fable.best_cli_path.cache_clear()
    yield
    fable.best_cli_path.cache_clear()


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 400 Claude Code 2.1.205 does not support this model; "
        "version 2.1.251 or newer is required.",
        "model_not_found",
        '[claude-code:unrecognized_model] {"model":"claude-fable-9-9"}',
        "There's an issue with the selected model (x). It may not exist.",
    ],
)
def test_unrunnable_model_is_its_own_kind(text):
    """Both transports must name this failure the same way — the ladder keys off
    it, and it is a local-build fact, not a sign the backend is unwell."""
    assert http_error_detail(label="Fable SDK", error=text)[0] == "model_unavailable"
    assert cli_error_detail(label="Fable", returncode=1, stderr=text)[0] == "model_unavailable"


def test_unrunnable_model_does_not_trip_the_breaker():
    """Otherwise the actionable "update Claude Code" message would be replaced by
    `circuit_open` after a few turns, and the ladder could never re-probe."""
    assert "model_unavailable" in health._NON_HEALTH_KINDS


def test_a_real_api_failure_is_still_a_plain_error():
    assert http_error_detail(label="Fable SDK", error="500 overloaded")[0] == "sdk_error"
    assert http_error_detail(label="Fable SDK", error="HTTP 401 bad key")[0] == "auth_failed"
    assert http_error_detail(label="Fable SDK", error="rate limit")[0] == "rate_limit"


# --- the ladder -----------------------------------------------------------


def test_prefers_the_newest_candidate():
    assert fable.FABLE_CANDIDATES[0] == fable.FABLE_PREFERRED_MODEL
    assert fable.FABLE_CANDIDATES[-1] == fable.FABLE_MODEL
    assert fable.fable_model() == fable.FABLE_PREFERRED_MODEL
    assert fable.fable_spec().key == "fable"


def test_an_operator_pin_wins_outright(monkeypatch):
    monkeypatch.setenv(fable.MODEL_ENV, "claude-fable-5")
    assert fable.fable_model() == "claude-fable-5"


def _stub_dispatch(monkeypatch, unrunnable: set[str]):
    """Answer OK for any model except those the 'transport' rejects."""
    seen: list[str] = []

    async def fake(message, timeout, resume, system_prompt, on_think, spec, use_cli):
        seen.append(spec.model)
        if spec.model in unrunnable:
            return OracleResult(
                "error", kind="model_unavailable", model=spec.model,
                text=f"Fable SDK request failed: {spec.model} does not support this model",
            )
        return OracleResult("ok", text="answered", model=spec.model)

    monkeypatch.setattr(fable, "_dispatch", fake)
    return seen


def test_falls_back_when_the_preferred_model_is_not_runnable(monkeypatch):
    seen = _stub_dispatch(monkeypatch, {fable.FABLE_PREFERRED_MODEL})
    res = _run(fable.run("How does routing work here?"))
    assert res.status == "ok" and res.model == fable.FABLE_MODEL
    assert seen == [fable.FABLE_PREFERRED_MODEL, fable.FABLE_MODEL]


def test_the_demotion_sticks_so_later_turns_skip_the_dead_rung(monkeypatch):
    seen = _stub_dispatch(monkeypatch, {fable.FABLE_PREFERRED_MODEL})
    _run(fable.run("First question about the router."))
    seen.clear()
    res = _run(fable.run("And the error path?"))
    assert res.status == "ok"
    assert seen == [fable.FABLE_MODEL]  # no second doomed probe
    assert fable.fable_model() == fable.FABLE_MODEL


def test_the_last_rung_reports_its_own_failure(monkeypatch):
    """Nothing left to fall back to — surface the error instead of looping."""
    seen = _stub_dispatch(monkeypatch, set(fable.FABLE_CANDIDATES))
    res = _run(fable.run("Trace the call path."))
    assert res.status == "error" and res.kind == "model_unavailable"
    assert seen == list(fable.FABLE_CANDIDATES)


def test_a_pinned_model_never_silently_answers_as_another(monkeypatch):
    """A pin is a claim about which model answered — falling back would make an
    A/B, or a trace that names the model, a lie."""
    monkeypatch.setenv(fable.MODEL_ENV, fable.FABLE_PREFERRED_MODEL)
    seen = _stub_dispatch(monkeypatch, {fable.FABLE_PREFERRED_MODEL})
    res = _run(fable.run("Where is the router defined?"))
    assert res.status == "error" and seen == [fable.FABLE_PREFERRED_MODEL]


def test_an_explicit_spec_never_falls_back(monkeypatch):
    seen = _stub_dispatch(monkeypatch, {fable51.FABLE51_MODEL})
    res = _run(fable51.run("Where is the router defined?"))
    assert res.status == "error" and seen == [fable51.FABLE51_MODEL]


def test_an_ordinary_failure_does_not_ladder(monkeypatch):
    """Only an unrunnable model steps down; a 500 must not burn a rung."""
    seen: list[str] = []

    async def fake(message, timeout, resume, system_prompt, on_think, spec, use_cli):
        seen.append(spec.model)
        return OracleResult("error", kind="sdk_error", text="500 overloaded")

    monkeypatch.setattr(fable, "_dispatch", fake)
    res = _run(fable.run("How does routing work here?"))
    assert res.kind == "sdk_error" and seen == [fable.FABLE_PREFERRED_MODEL]
    assert fable.fable_model() == fable.FABLE_PREFERRED_MODEL  # rung intact


# --- which `claude` binary the SDK spawns ---------------------------------


def _stub_binaries(monkeypatch, *, system, bundled, versions):
    monkeypatch.setattr(fable.shutil, "which", lambda b: system)
    monkeypatch.setattr(fable, "_bundled_cli", lambda: bundled)
    monkeypatch.setattr(fable, "_cli_version", lambda p: versions.get(p))


def test_prefers_the_path_binary_when_it_is_newer(monkeypatch):
    """The SDK vendors its own Claude Code and prefers it over PATH; that copy
    only moves on an SDK upgrade, so it is the thing most likely to be too old
    for a new model."""
    _stub_binaries(monkeypatch, system="/usr/bin/claude", bundled="/pkg/claude",
                   versions={"/usr/bin/claude": (2, 1, 258), "/pkg/claude": (2, 1, 205)})
    assert fable.best_cli_path() == "/usr/bin/claude"


def test_leaves_the_sdk_alone_when_its_bundle_is_current(monkeypatch):
    _stub_binaries(monkeypatch, system="/usr/bin/claude", bundled="/pkg/claude",
                   versions={"/usr/bin/claude": (2, 1, 205), "/pkg/claude": (2, 1, 258)})
    assert fable.best_cli_path() is None


def test_cli_path_is_overridable_and_degrades_quietly(monkeypatch):
    monkeypatch.setenv(fable.CLI_ENV, "/opt/claude")
    assert fable.best_cli_path() == "/opt/claude"
    fable.best_cli_path.cache_clear()
    monkeypatch.delenv(fable.CLI_ENV)
    # nothing on PATH, and an unprobeable binary → let the SDK decide
    _stub_binaries(monkeypatch, system=None, bundled="/pkg/claude", versions={})
    assert fable.best_cli_path() is None


# --- the `fable51` token --------------------------------------------------


def test_registered_as_a_pinned_council_oracle():
    assert "fable51" in oracles.KNOWN
    assert oracles.label("fable51") == fable.FABLE_PREFERRED_MODEL
    assert oracles.available("fable51") is True  # same OAuth session as fable
    assert oracles.resolve(["minimax", "fable51", "fable"]) == (
        ["fable", "fable51", "minimax"], [],
    )
    for alias in ("fable5.1", "FABLE-5.1", "claude-fable-5-1"):
        assert oracles.resolve_ordered([alias]) == (["fable51"], []), alias


def test_label_of_fable_follows_the_ladder(monkeypatch):
    assert oracles.label("fable") == fable.FABLE_PREFERRED_MODEL
    monkeypatch.setenv(fable.MODEL_ENV, "claude-fable-5")
    assert oracles.label("fable") == "claude-fable-5"


def test_dispatches_to_the_pinned_bridge(monkeypatch):
    async def fake(q, c="", **kw):
        return OracleResult("ok", text="51 ans")

    monkeypatch.setattr(oracles.fable51, "run", fake)
    r = _run(oracles.run("fable51", "q"))
    assert r.key == "fable51" and r.text == "51 ans"
    assert r.model == fable.FABLE_PREFERRED_MODEL


def test_tier_presets_skip_the_pinned_twin(monkeypatch):
    """`fable51` is the same tier, price, and (today) the same model as `fable` —
    a blanket fan-out that included both would pay twice for one voice."""
    monkeypatch.setattr(oracles.ollama, "council_models", lambda: [])
    assert "fable51" not in oracles.tier_models("middle")
    assert "fable51" not in oracles.tier_models("full")
    assert "fable51" not in oracles.default_models()
    assert "fable" in oracles.tier_models("middle")


# --- what a failed SDK turn reports ---------------------------------------


class _Result:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_the_apis_own_sentence_survives():
    """The 400 that blocks a too-new model names the fix ("update Claude Code").
    Reporting the status or subtype instead turned it into "unknown"."""
    msg = _Result(
        is_error=True, subtype="success", result=(
            "API Error: 400 Claude Code 2.1.205 does not support this model; "
            "version 2.1.251 or newer is required."
        ),
    )
    detail = fable._result_error(msg)
    assert "does not support this model" in detail
    # ...and that text is what makes the failure classifiable, hence recoverable
    assert http_error_detail(label="Fable SDK", error=detail)[0] == "model_unavailable"


def test_the_sentence_outranks_an_earlier_placeholder():
    """The AssistantMessage that precedes this failure carries `error="unknown"`
    and the ResultMessage's own subtype is "success" — take either and the real
    diagnosis is lost."""
    msg = _Result(is_error=True, subtype="success", api_error_status=400,
                  result="API Error: 400 ... does not support this model; version X or newer")
    assert "does not support this model" in fable._result_error(msg, current="unknown")


def test_falls_back_through_status_then_subtype():
    assert fable._result_error(_Result(is_error=True, api_error_status="500")) == "500"
    assert fable._result_error(_Result(is_error=True, subtype="error_during_execution")) == (
        "error_during_execution"
    )
    assert fable._result_error(_Result(is_error=True)) == "result error"
    # an error already collected is kept when the result message adds nothing
    assert fable._result_error(_Result(is_error=True), current="boom") == "boom"
    # a blank/non-string `result` must not shadow the status
    assert fable._result_error(_Result(is_error=True, result="  ", api_error_status="429")) == "429"


# --- what the `ask` tool reports ------------------------------------------


def test_ask_credits_the_model_that_actually_answered(monkeypatch):
    """A mid-call demotion must not leave `ask` reporting the rung it started on
    — the payload, the audit row and the hub turn all read from this."""
    import ask_fable.server as server
    from ask_fable.sessions import SessionStore

    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    recorded: list[str] = []
    monkeypatch.setattr(server.audit, "record", lambda **kw: recorded.append(kw.get("model")))
    mirrored: list[str] = []
    monkeypatch.setattr(server, "_hub_mirror", lambda **kw: mirrored.append(kw.get("oracle")))

    async def fell_back(question, context="", *, resume=None, **kw):
        return OracleResult("ok", text="answered", model=fable.FABLE_MODEL)

    monkeypatch.setattr(server.fable, "run", fell_back)
    out = _run(server._handle_ask(SessionStore(), {"question": "How does routing work here?"}))
    assert fable.fable_model() == fable.FABLE_PREFERRED_MODEL  # asked for the top rung
    assert out["model"] == fable.FABLE_MODEL  # but reports the one that answered
    assert recorded == [fable.FABLE_MODEL] and mirrored == [fable.FABLE_MODEL]


def test_the_fallback_does_not_resume_the_demoted_models_session(monkeypatch):
    """An SDK session id belongs to the model that made it — resuming a Fable 5.1
    thread on Fable 5 is the cross-model resume `ask_opus5` namespaces to avoid."""
    seen: list[tuple[str, str | None]] = []

    async def fake(message, timeout, resume, system_prompt, on_think, spec, use_cli):
        seen.append((spec.model, resume))
        if spec.model == fable.FABLE_PREFERRED_MODEL:
            return OracleResult("error", kind="model_unavailable", text="too old")
        return OracleResult("ok", text="answered", model=spec.model)

    monkeypatch.setattr(fable, "_dispatch", fake)
    _run(fable.run("And the error path?", resume="sid-from-5-1"))
    assert seen == [(fable.FABLE_PREFERRED_MODEL, "sid-from-5-1"), (fable.FABLE_MODEL, None)]
