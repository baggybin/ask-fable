"""OpenRouter as an oracle backend.

Four layers: config precedence, the catalog/ranking built on OpenRouter's own
published metadata, the effort clamp that replaces Atlas's blind probe-and-retry,
and the registry/tool wiring. No network call is ever made.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error

import pytest

import ask_fable.openrouter as orr
import ask_fable.oracles as oracles
import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    for var in (
        "ASK_FABLE_OPENROUTER_API_KEY", "OPENROUTER_API_KEY",
        "ASK_FABLE_OPENROUTER_MODEL", "ASK_FABLE_OPENROUTER_COUNCIL",
        "ASK_FABLE_OPENROUTER_EFFORT", "ASK_FABLE_EFFORT",
        "ASK_FABLE_OPENROUTER_SYNTHESIZER", "ASK_FABLE_OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(orr.config, "load", lambda: {})
    orr._EFFORT_CACHE.clear()
    yield
    orr._EFFORT_CACHE.clear()


def _row(mid, *, out_price="0.000010", ctx=1_000_000, efforts=("high", "low"), created=None,
         modalities=("text",), params=("reasoning_effort",)):
    return {
        "id": mid,
        "name": mid,
        "created": created,
        "context_length": ctx,
        "description": "a model",
        "architecture": {"input_modalities": ["text"], "output_modalities": list(modalities)},
        "pricing": {"prompt": "0.000001", "completion": out_price},
        "supported_parameters": list(params),
        "reasoning": ({"supported_efforts": list(efforts), "default_effort": "medium"}
                      if efforts else {}),
    }


# --- config ---------------------------------------------------------------


def test_key_precedence_and_configured(monkeypatch):
    assert orr.api_key() is None and orr.configured() is False
    # the conventional name most OpenRouter tooling already sets is accepted...
    monkeypatch.setenv("OPENROUTER_API_KEY", "generic")
    assert orr.api_key() == "generic"
    # ...but ask_fable's own name wins, so one server can differ from the shell
    monkeypatch.setenv("ASK_FABLE_OPENROUTER_API_KEY", "ours")
    assert orr.api_key() == "ours" and orr.configured() is True


def test_model_and_council_precedence(monkeypatch):
    assert orr.default_model() == orr.DEFAULT_OPENROUTER_MODEL
    monkeypatch.setenv("ASK_FABLE_OPENROUTER_MODEL", "openai/gpt-5.6-sol")
    assert orr.default_model() == "openai/gpt-5.6-sol"
    monkeypatch.setattr(orr.config, "get_str", lambda k: "z/from-config" if k == "openrouter_model" else None)
    assert orr.default_model() == "z/from-config"  # config file outranks env

    monkeypatch.setattr(orr.config, "get_str", lambda k: None)
    monkeypatch.setattr(orr.config, "get_list", lambda k: [])
    monkeypatch.setenv("ASK_FABLE_OPENROUTER_COUNCIL", "a/one, b/two  a/ONE")
    assert orr.council_models() == ["a/one", "b/two"]  # comma/space list, de-duped


# --- catalog --------------------------------------------------------------


def test_unusable_rows_are_filtered():
    """Batch endpoints are async and priced differently; alias rows point at another
    row; image generators are not reasoning oracles."""
    assert orr._model_item(_row("vendor/m:batch")) is None
    assert orr._model_item(_row("~vendor/m-latest")) is None
    assert orr._model_item(_row("vendor/img", modalities=("image",))) is None
    assert orr._model_item(_row("vendor/good"))["model_id"] == "vendor/good"


def test_per_token_prices_become_per_million():
    it = orr._model_item(_row("v/m", out_price="0.00005"))
    assert it["input_per_m"] == pytest.approx(1.0)
    assert it["output_per_m"] == pytest.approx(50.0)
    assert it["cost_note"] == "$1.00/$50.00 per M"


def test_free_models_are_labelled():
    row = _row("v/free")
    row["pricing"] = {"prompt": "0", "completion": "0"}
    it = orr._model_item(row)
    assert it["cost_note"] == "free" and "FREE" in it["tags"]


def test_default_ranking_leads_with_capable_not_cheap():
    """Price is the only capability proxy the catalog publishes — nobody charges
    $50/M for a weak model. Ranking on cheapness by default would put a tiny free
    model at the top of an ENGINEERING oracle's menu."""
    items = [
        orr._model_item(_row("cheap/tiny", out_price="0.0000001", ctx=8000)),
        orr._model_item(_row("frontier/big", out_price="0.00005", ctx=1_000_000)),
    ]
    assert orr.recommend_models(items, "", limit=1)[0]["model_id"] == "frontier/big"


def test_asking_for_cheap_flips_the_ranking():
    items = [
        orr._model_item(_row("cheap/tiny", out_price="0.0000001", ctx=200_000)),
        orr._model_item(_row("frontier/big", out_price="0.00005", ctx=1_000_000)),
    ]
    top = orr.recommend_models(items, "cheap high-volume summarizing", limit=1)[0]
    assert top["model_id"] == "cheap/tiny"


def test_shortlist_is_provider_diverse():
    items = [orr._model_item(_row(f"same/m{i}", out_price="0.00005")) for i in range(4)]
    items.append(orr._model_item(_row("other/m", out_price="0.00004")))
    picked = orr.recommend_models(items, "", limit=2)
    assert {p["provider"] for p in picked} == {"same", "other"}


def test_catalog_degrades_without_network(monkeypatch):
    monkeypatch.setattr(orr, "_get_json", lambda *a, **k: None)
    cat = orr.catalog()
    assert cat["cloud_ok"] is False and cat["menu"] == []
    assert cat["effort_choices"]  # the effort picker still renders


# --- the effort clamp -----------------------------------------------------


def _stub_catalog(monkeypatch, rows):
    monkeypatch.setattr(orr, "_get_json", lambda *a, **k: {"data": rows})


def test_effort_is_clamped_to_what_the_model_accepts(monkeypatch):
    """Atlas has to send a guess and retry without it on a 400. OpenRouter
    publishes supported_efforts, so we ask for what the model takes."""
    _stub_catalog(monkeypatch, [_row("v/m", efforts=("medium", "low"))])
    assert orr._clamp_effort("v/m", "high") == "medium"  # asked above the ceiling


def test_supported_effort_passes_through(monkeypatch):
    _stub_catalog(monkeypatch, [_row("v/m", efforts=("xhigh", "high", "low"))])
    assert orr._clamp_effort("v/m", "high") == "high"


def test_non_reasoning_model_gets_no_effort_field(monkeypatch):
    _stub_catalog(monkeypatch, [_row("v/plain", efforts=())])
    assert orr._clamp_effort("v/plain", "high") is None


def test_unknown_model_omits_rather_than_guesses(monkeypatch):
    """A catalog miss must not cost a turn: omit the field instead of risking a
    rejected one."""
    _stub_catalog(monkeypatch, [_row("v/other")])
    assert orr._clamp_effort("v/unlisted", "high") is None
    monkeypatch.setattr(orr, "_get_json", lambda *a, **k: None)
    orr._EFFORT_CACHE.clear()
    assert orr._clamp_effort("v/m", "high") is None


# --- run() ----------------------------------------------------------------


def test_run_without_a_key_is_a_config_state():
    r = _run(orr.run("v/m", "How does routing work here?"))
    assert r.status == "error" and r.kind == "not_configured"
    assert "ASK_FABLE_OPENROUTER_API_KEY" in r.text


def _stub_post(monkeypatch, payload):
    class _Resp:
        def __init__(self, body): self._b = body
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        orr.urllib.request, "urlopen",
        lambda req, timeout=None: _Resp(json.dumps(payload).encode()),
    )


def test_run_reports_real_cost_and_the_upstream_provider(monkeypatch):
    """OpenRouter bills the call and tells us what it cost and who served it —
    most backends can only be estimated from a price table."""
    monkeypatch.setenv("ASK_FABLE_OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(orr, "_clamp_effort", lambda *a: None)
    _stub_post(monkeypatch, {
        "model": "v/m", "provider": "Venice",
        "choices": [{"finish_reason": "stop",
                     "message": {"content": "answered", "reasoning": "because"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                  "cost": 0.0023, "prompt_tokens_details": {"cached_tokens": 4}},
    })
    r = _run(orr.run("v/m", "How does routing work here?"))
    assert r.status == "ok" and r.text == "answered"
    assert r.thinking == "because"
    assert r.telemetry.actual_model == "v/m (via Venice)"
    assert r.telemetry.usage.cost_usd == pytest.approx(0.0023)
    assert r.telemetry.usage.cache_read_input_tokens == 4
    assert r.telemetry.stop_reason == "stop"


def test_http_failure_is_classified(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(orr, "_clamp_effort", lambda *a: None)

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(orr.urllib.request, "urlopen", boom)
    r = _run(orr.run("v/m", "How does routing work here?"))
    assert r.status == "error" and r.kind == "auth_failed"  # a bad key is a CONFIG state


# --- registry + tool wiring ----------------------------------------------


def test_openrouter_tokens_resolve():
    assert oracles.openrouter_model("openrouter:openai/gpt-5") == "openai/gpt-5"
    assert oracles.openrouter_model("atlas:openai/gpt-5") is None
    assert oracles.label("openrouter:openai/gpt-5") == "openai/gpt-5"
    got, unknown = oracles.resolve(["fable", "openrouter:openai/gpt-5"])
    assert got == ["fable", "openrouter:openai/gpt-5"] and unknown == []


def test_gateway_ids_keep_their_casing():
    """Model ids are case-sensitive at the gateway; lowercasing one turns a valid
    model into a 404."""
    got, _ = oracles.resolve_ordered(["OpenRouter:DeepSeek/DeepSeek-V4-Pro"])
    assert got == ["openrouter:DeepSeek/DeepSeek-V4-Pro"]


def test_dispatch_reaches_the_openrouter_bridge(monkeypatch):
    async def fake(model, q, c="", **kw):
        return OracleResult("ok", text=f"answered by {model}")

    monkeypatch.setattr(oracles.openrouter, "run", fake)
    monkeypatch.setattr(oracles.openrouter, "configured", lambda: True)
    monkeypatch.setattr(oracles.grok, "available", lambda: False)
    monkeypatch.setattr(oracles.kimi, "available", lambda: False)
    r = _run(oracles.run("openrouter:openai/gpt-5", "q"))
    assert r.key == "openrouter:openai/gpt-5" and "answered by openai/gpt-5" in r.text


def test_grok_and_kimi_ids_prefer_the_local_cli(monkeypatch):
    """A model the operator can already serve from an authenticated CLI must not
    be billed per token by the gateway — the same rule Atlas applies."""
    seen: list[str] = []

    async def fake_grok(q, c="", **kw):
        seen.append("grok")
        return OracleResult("ok", text="grok")

    async def fake_kimi(q, c="", **kw):
        seen.append("kimi")
        return OracleResult("ok", text="kimi")

    monkeypatch.setattr(oracles.grok, "available", lambda: True)
    monkeypatch.setattr(oracles.grok, "run", fake_grok)
    monkeypatch.setattr(oracles.kimi, "available", lambda: True)
    monkeypatch.setattr(oracles.kimi, "run", fake_kimi)
    # OpenRouter spells the vendor `x-ai/`, Atlas spells it `xai/` — both reroute
    _run(oracles.run("openrouter:x-ai/grok-4.6", "q"))
    _run(oracles.run("openrouter:moonshotai/kimi-k3", "q"))
    assert seen == ["grok", "kimi"]


def test_tools_are_registered():
    assert server._TOOL_SCHEMAS["ask_openrouter"] is server._OPENROUTER_SCHEMA
    assert server._TOOL_SCHEMAS["list_openrouter_models"] is server._LIST_OPENROUTER_SCHEMA
    assert server._schema_error("ask_openrouter", {"question": "q", "model": "a/b"}) is None
    assert server._schema_error("ask_openrouter", {"question": "q", "effort": "insane"})


# --- council + configure --------------------------------------------------


def _panel(monkeypatch):
    async def fake_oracle(key, question, context="", **kw):
        return OracleResult("ok", key=key, text=f"{key} says", model=key)

    async def fake_fable(question, context="", *, resume=None, system_prompt=None, **kw):
        return OracleResult("ok", text="MERGED")

    async def fake_synthesis(key, prompt, **kw):
        return OracleResult("ok", key=key, text="MERGED", model=key)

    monkeypatch.setattr(server.oracles, "run", fake_oracle)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    # The adjudicator ladder picks the local `codex` CLI whenever its BINARY is
    # on PATH, so without these two the tests shell out to the real thing:
    # ~8s per test, and a different code path on a machine that lacks it.
    monkeypatch.setattr(server.oracles, "run_synthesis", fake_synthesis)
    monkeypatch.setattr(server.oracles, "available", lambda key: key != "codex")
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


def test_council_prefixes_bare_ids_and_preserves_casing(monkeypatch):
    _panel(monkeypatch)
    monkeypatch.setattr(server.openrouter, "configured", lambda: True)
    seen: list[str] = []

    async def spy(key, question, context="", **kw):
        seen.append(key)
        return OracleResult("ok", key=key, text="x", model=key)

    monkeypatch.setattr(server.oracles, "run", spy)
    _run(server._handle_openrouter_council({
        "question": "How does routing work here?",
        "models": ["DeepSeek/DeepSeek-V4-Pro", "openrouter:openai/gpt-5"],
    }))
    assert seen == ["openrouter:DeepSeek/DeepSeek-V4-Pro", "openrouter:openai/gpt-5"]


def test_council_without_a_key_is_a_config_state(monkeypatch):
    _panel(monkeypatch)
    monkeypatch.setattr(server.openrouter, "configured", lambda: False)
    monkeypatch.setattr(server.grok, "available", lambda: False)
    monkeypatch.setattr(server.kimi, "available", lambda: False)
    out = _run(server._handle_openrouter_council(
        {"question": "How does routing work here?", "models": ["openai/gpt-5"]}
    ))
    assert out["status"] == "error" and out["kind"] == "not_configured"


def test_an_all_local_panel_runs_without_a_key(monkeypatch):
    """A panel of only Grok/Kimi members reroutes to the operator's CLIs, so it
    must not be blocked for want of a gateway key."""
    _panel(monkeypatch)
    monkeypatch.setattr(server.openrouter, "configured", lambda: False)
    monkeypatch.setattr(server.grok, "available", lambda: True)
    monkeypatch.setattr(server.kimi, "available", lambda: True)
    out = _run(server._handle_openrouter_council({
        "question": "How does routing work here?",
        "models": ["x-ai/grok-4.6", "moonshotai/kimi-k3"],
    }))
    assert out["status"] == "ok"


def test_council_needs_models_from_somewhere(monkeypatch):
    _panel(monkeypatch)
    monkeypatch.setattr(server.openrouter, "council_models", lambda: [])
    monkeypatch.setattr(server.openrouter, "catalog", lambda *a, **k: {"cloud_ok": False})
    out = _run(server._handle_openrouter_council({"question": "How does routing work here?"}))
    assert out["status"] == "error" and out["kind"] == "no_models"


def test_configure_persists_council_and_synthesizer(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(server.config, "save", lambda patch: saved.update(patch) or "/cfg.json")
    monkeypatch.setattr(server.openrouter, "council_models", lambda: ["a/one"])
    monkeypatch.setattr(server.openrouter, "synthesizer_token", lambda: "codex")
    out = server._handle_configure_openrouter(
        {"models": ["a/one", "A/ONE", "b/two"], "synthesizer": "gpt"}
    )
    assert out["status"] == "ok"
    assert saved["openrouter_council"] == ["a/one", "b/two"]  # de-duped
    assert saved["openrouter_synthesizer"] == "codex"  # 'gpt' resolves to the codex token


def test_configure_rejects_an_empty_patch():
    out = server._handle_configure_openrouter({})
    assert out["status"] == "error" and out["kind"] == "bad_args"


def test_council_tools_are_registered():
    assert server._TOOL_SCHEMAS["ask_openrouter_council"] is server._OPENROUTER_COUNCIL_SCHEMA
    assert (
        server._TOOL_SCHEMAS["configure_openrouter_council"]
        is server._CONFIGURE_OPENROUTER_SCHEMA
    )


@pytest.mark.parametrize(
    "tool,args",
    [
        ("ask_council", {"models": ["openrouter:openai/gpt-5"]}),
        ("ask_council", {"synthesizer": "openrouter:openai/gpt-5.6-sol"}),
        ("ask_chain", {"models": ["minimax", "openrouter:openai/gpt-5"]}),
        ("ask_debate", {"adjudicator": "openrouter:openai/gpt-5"}),
        ("ask_openrouter_council", {"models": ["a/b"], "synthesizer": "openrouter:a/b"}),
    ],
)
def test_multi_model_schemas_accept_openrouter_tokens(tool, args):
    """A token the handler accepts must not be rejected client-side by a
    strictly-validating MCP host — the declared schema and the handler have to
    agree on the vocabulary."""
    assert server._schema_error(tool, {"question": "How does routing work here?", **args}) is None


def test_a_typod_model_id_is_not_backend_ill_health(monkeypatch):
    """A gateway 400 for an unknown id is the caller's mistake. Classifying it as
    a generic sdk_error would push the circuit breaker toward open and take the
    whole provider down over a typo."""
    monkeypatch.setenv("ASK_FABLE_OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(orr, "_clamp_effort", lambda *a: None)

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 400,
            'no/such-model is not a valid model ID', {}, None,
        )

    monkeypatch.setattr(orr.urllib.request, "urlopen", boom)
    r = _run(orr.run("no/such-model", "How does routing work here?"))
    assert r.kind == "model_unavailable"
    import ask_fable.health as health
    assert "model_unavailable" in health._NON_HEALTH_KINDS
