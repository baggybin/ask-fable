"""ask_fable oracle registry — resolve / label / available / run mapping."""

from __future__ import annotations

import asyncio

import ask_fable.oracles as oracles
from ask_fable.oracle_common import OracleResult


def _run(coro):
    return asyncio.run(coro)


def test_resolve_default_and_dedup_and_order():
    assert oracles.resolve(None) == (["fable", "minimax"], [])
    assert oracles.resolve([]) == (["fable", "minimax"], [])
    # de-dupes, lowercases, keeps canonical order, splits unknowns
    known, unknown = oracles.resolve(["glm", "DeepSeek", "glm", "gpt5"])
    assert known == ["deepseek", "glm"] and unknown == ["gpt5"]
    # all-unknown falls back to the default council
    assert oracles.resolve(["nope"]) == (["fable", "minimax"], ["nope"])
    # canonical order is cheap-first: direct APIs before subscription CLIs
    assert oracles.resolve(["codex", "glm", "deepseek"]) == (["deepseek", "glm", "codex"], [])


def test_resolve_fallback_grows_with_deepseek_key(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_DEEPSEEK_API_KEY", "k")
    assert oracles.resolve(None) == (["fable", "deepseek", "minimax"], [])
    assert oracles.resolve(["nope"]) == (["fable", "deepseek", "minimax"], ["nope"])


def test_default_models_availability(monkeypatch):
    monkeypatch.delenv("ASK_FABLE_DEEPSEEK_API_KEY", raising=False)
    assert oracles.default_models() == ["fable", "minimax"]
    monkeypatch.setenv("ASK_FABLE_DEEPSEEK_API_KEY", "k")
    assert oracles.default_models() == ["fable", "deepseek", "minimax"]


def test_resolve_ordered_preserves_order_dups_and_aliases():
    # order is the computation and repeats are legitimate (draft > critique > re-decide)
    assert oracles.resolve_ordered(["fable", "glm", "fable"]) == (["fable", "glm", "fable"], [])
    # 'm3' aliases to 'minimax'; order preserved, not canonicalized like resolve()
    assert oracles.resolve_ordered(["m3", "glm", "fable"]) == (["minimax", "glm", "fable"], [])
    # unknowns split out, in order; empty falls back to empty (caller supplies default)
    known, unknown = oracles.resolve_ordered(["minimax", "bogus", "deepseek"])
    assert known == ["minimax", "deepseek"] and unknown == ["bogus"]
    assert oracles.resolve_ordered(None) == ([], [])


def test_resolve_applies_aliases_like_the_chain():
    # 'm3'/'gpt'/'xai' must resolve in a fan-out exactly as in a chain pipeline —
    # previously an aliased council silently fell back to the DEFAULT council.
    assert oracles.resolve(["m3"]) == (["minimax"], [])
    # aliases canonicalize into the KNOWN order (cheap-first), deduped against
    # their canonical spellings
    assert oracles.resolve(["gpt", "m3"]) == (["minimax", "codex"], [])
    assert oracles.resolve(["m3", "minimax"]) == (["minimax"], [])
    # an alias for an unknown backend still reports unknown, not a fallback
    known, unknown = oracles.resolve(["gpt5"])
    assert known == ["fable", "minimax"] and unknown == ["gpt5"]


def test_available(monkeypatch):
    assert oracles.available("fable") is True
    monkeypatch.setattr(oracles.shutil, "which", lambda b: "/usr/bin/mmx")
    assert oracles.available("minimax") is True
    monkeypatch.setattr(oracles.shutil, "which", lambda b: None)
    assert oracles.available("minimax") is False
    # gemini depends on the `gemini` CLI being on PATH
    monkeypatch.setattr(oracles.shutil, "which", lambda b: "/usr/bin/gemini")
    assert oracles.available("gemini") is True
    monkeypatch.setattr(oracles.shutil, "which", lambda b: None)
    assert oracles.available("gemini") is False
    # glm/deepseek depend on a configured key. glm additionally falls back to
    # Atlas-hosted GLM, so an Atlas key alone also makes it available — clear both
    # to assert the genuinely-unconfigured case.
    monkeypatch.delenv("ASK_FABLE_GLM_API_KEY", raising=False)
    monkeypatch.delenv("ASK_FABLE_ATLAS_API_KEY", raising=False)
    monkeypatch.delenv("ATLASCLOUD_API_KEY", raising=False)
    assert oracles.available("glm") is False
    monkeypatch.setenv("ASK_FABLE_GLM_API_KEY", "k")
    assert oracles.available("glm") is True


def test_run_wraps_fable_and_minimax(monkeypatch):
    async def fake_fable(q, c="", **kw):
        return OracleResult("ok", text="fable ans", thinking="th")

    async def fake_mmx(q, c="", **kw):
        return OracleResult("ok", text="mmx ans", model="MiniMax-M3")

    monkeypatch.setattr(oracles.fable, "run", fake_fable)
    monkeypatch.setattr(oracles.minimax, "run", fake_mmx)
    f = _run(oracles.run("fable", "q"))
    assert f.key == "fable" and f.model == oracles.fable.fable_model() and f.thinking == "th"
    m = _run(oracles.run("minimax", "q"))
    assert m.key == "minimax" and m.model == "MiniMax-M3" and m.text == "mmx ans"


def test_resolve_recognizes_gemini():
    known, unknown = oracles.resolve(["gemini", "minimax"])
    assert known == ["minimax", "gemini"] and unknown == []  # canonical KNOWN order


def test_run_wraps_gemini(monkeypatch):
    pass  # OracleResult imported at top

    async def fake_gemini(q, c="", **kw):
        return OracleResult("ok", text="gemini ans", model="gemini-3-pro", thinking="th")

    monkeypatch.setattr(oracles.gemini, "run", fake_gemini)
    g = _run(oracles.run("gemini", "q"))
    assert g.key == "gemini" and g.model == "gemini-3-pro" and g.text == "gemini ans"
    assert g.status == "ok" and g.thinking == "th"


def test_run_http_not_configured(monkeypatch):
    # No direct key AND no Atlas key -> genuinely unconfigured.
    monkeypatch.delenv("ASK_FABLE_GLM_API_KEY", raising=False)
    monkeypatch.delenv("ASK_FABLE_ATLAS_API_KEY", raising=False)
    monkeypatch.delenv("ATLASCLOUD_API_KEY", raising=False)
    r = _run(oracles.run("glm", "q"))
    assert r.status == "error" and r.kind == "not_configured" and "GLM_API_KEY" in r.text


def test_glm_falls_back_to_atlas_without_zai_key(monkeypatch):
    """No Z.ai key + an Atlas key -> glm is served by Atlas-hosted GLM, keeping
    its own oracle key for attribution."""
    monkeypatch.delenv("ASK_FABLE_GLM_API_KEY", raising=False)
    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "atlas-key")
    seen = {}

    async def fake_atlas(model, q, c="", **kw):
        seen["model"] = model
        return OracleResult("ok", text="glm via atlas", model=model)

    monkeypatch.setattr(oracles.atlas, "run", fake_atlas)
    assert oracles.available("glm") is True
    assert oracles.label("glm") == oracles._ATLAS_FALLBACK["glm"]
    r = _run(oracles.run("glm", "q"))
    assert r.status == "ok" and r.text == "glm via atlas"
    assert r.key == "glm"  # attribution stays on the oracle, not the atlas token
    assert seen["model"] == oracles._ATLAS_FALLBACK["glm"]


def test_glm_prefers_direct_zai_key_over_atlas(monkeypatch):
    """The direct endpoint is cheaper, so a configured Z.ai key always wins."""
    monkeypatch.setenv("ASK_FABLE_GLM_API_KEY", "zai-key")
    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "atlas-key")

    async def fake_atlas(*a, **k):
        raise AssertionError("atlas must not be called when the Z.ai key is set")

    async def fake_http(cfg, q, c="", **kw):
        return OracleResult("ok", text="glm direct", model=cfg.model)

    monkeypatch.setattr(oracles.atlas, "run", fake_atlas)
    monkeypatch.setattr(oracles.anthropic_http, "run", fake_http)
    r = _run(oracles.run("glm", "q"))
    assert r.status == "ok" and r.text == "glm direct" and r.key == "glm"


def test_deepseek_has_no_atlas_fallback(monkeypatch):
    """Only glm opts into the fallback — deepseek still reports not_configured."""
    monkeypatch.delenv("ASK_FABLE_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "atlas-key")
    assert oracles.available("deepseek") is False
    r = _run(oracles.run("deepseek", "q"))
    assert r.status == "error" and r.kind == "not_configured"


def test_run_unknown_oracle():
    r = _run(oracles.run("banana", "q"))
    assert r.status == "error" and r.kind == "unknown_oracle"


def test_resolve_ollama_tokens():
    # ollama:<model> tokens are recognized (not unknown), after KNOWN, in order
    known, unknown = oracles.resolve(
        ["fable", "ollama:kimi-k2.7-code:cloud", "ollama:gpt-oss:120b-cloud", "nope"]
    )
    assert known == ["fable", "ollama:kimi-k2.7-code:cloud", "ollama:gpt-oss:120b-cloud"]
    assert unknown == ["nope"]
    # de-dupes ollama tokens
    known, _ = oracles.resolve(["ollama:x:cloud", "ollama:x:cloud"])
    assert known == ["ollama:x:cloud"]
    # a bare ollama: (no model) is unknown, falls back to DEFAULT
    assert oracles.resolve(["ollama:"]) == (["fable", "minimax"], ["ollama:"])


def test_label_and_available_ollama(monkeypatch):
    assert oracles.label("ollama:kimi-k2.7-code:cloud") == "kimi-k2.7-code:cloud"
    # default (local daemon) is reachable via signin — available without a key
    monkeypatch.delenv("ASK_FABLE_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("ASK_FABLE_OLLAMA_BASE_URL", raising=False)
    assert oracles.available("ollama:gpt-oss:120b-cloud") is True
    # remote endpoint without a key -> unavailable
    monkeypatch.setenv("ASK_FABLE_OLLAMA_BASE_URL", "https://ollama.com")
    assert oracles.available("ollama:gpt-oss:120b-cloud") is False
    # remote + key -> available
    monkeypatch.setenv("ASK_FABLE_OLLAMA_API_KEY", "k")
    assert oracles.available("ollama:gpt-oss:120b-cloud") is True


def test_run_ollama_dispatch(monkeypatch):
    pass  # OracleResult imported at top

    async def fake_ollama(model, q, c="", **kw):
        return OracleResult("ok", text=f"ans from {model}", model=model, thinking="th")

    monkeypatch.setenv("ASK_FABLE_OLLAMA_API_KEY", "k")
    monkeypatch.setattr(oracles.ollama, "run", fake_ollama)
    r = _run(oracles.run("ollama:kimi:cloud", "q"))
    # key keeps the full token; model is the bare id
    assert r.key == "ollama:kimi:cloud" and r.model == "kimi:cloud"
    assert r.status == "ok" and r.text == "ans from kimi:cloud" and r.thinking == "th"


def test_run_ollama_not_configured(monkeypatch):
    # remote endpoint + no key -> not_configured (local default would be reachable)
    monkeypatch.delenv("ASK_FABLE_OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("ASK_FABLE_OLLAMA_BASE_URL", "https://ollama.com")
    r = _run(oracles.run("ollama:kimi:cloud", "q"))
    assert r.status == "error" and r.kind == "not_configured" and "OLLAMA_API_KEY" in r.text


def test_tier_models(monkeypatch):
    monkeypatch.setattr(oracles.ollama, "council_models", lambda: ["nemotron-3-ultra:cloud", "qwen3-coder:480b-cloud"])
    assert oracles.tier_models("default") == ["fable", "minimax"]
    # middle/full list every KNOWN member unconditionally, cheap-first
    assert oracles.tier_models("middle") == [
        "fable", "opus", "deepseek", "minimax", "glm", "gemini", "codex", "grok", "kimi",
    ]
    assert oracles.tier_models("full") == [
        "fable", "opus", "deepseek", "minimax", "glm", "gemini", "codex", "grok", "kimi",
        "ollama:nemotron-3-ultra:cloud", "ollama:qwen3-coder:480b-cloud",
    ]
    # unknown tier falls back to default
    assert oracles.tier_models("bogus") == ["fable", "minimax"]
    # the default tier (and the bogus-tier fallback) grow deepseek when its key is set
    monkeypatch.setenv("ASK_FABLE_DEEPSEEK_API_KEY", "k")
    assert oracles.tier_models("default") == ["fable", "deepseek", "minimax"]
    assert oracles.tier_models("bogus") == ["fable", "deepseek", "minimax"]


def test_resolve_preserves_atlas_id_casing():
    # Atlas ids are case-SENSITIVE (e.g. deepseek-ai/DeepSeek-V3.1-Terminus);
    # names/aliases/prefixes stay case-insensitive, the model part is verbatim.
    known, unknown = oracles.resolve(
        ["ATLAS:deepseek-ai/DeepSeek-V3.1-Terminus", "Fable",
         "atlas:DEEPSEEK-ai/deepseek-v3.1-terminus"]  # case-insensitive dup
    )
    assert known == ["fable", "atlas:deepseek-ai/DeepSeek-V3.1-Terminus"]
    assert unknown == []


def test_resolve_ordered_preserves_atlas_id_casing():
    recognized, unknown = oracles.resolve_ordered(
        ["GPT", "atlas:Qwen/Qwen3-235B-A22B-Instruct-2507"]
    )
    assert recognized == ["codex", "atlas:Qwen/Qwen3-235B-A22B-Instruct-2507"]
    assert unknown == []


def test_run_synthesis_fable_uses_system_channel(monkeypatch):
    seen = {}

    async def fake_fable(question, context="", *, resume=None, system_prompt=None, on_think=None):
        seen["question"], seen["system"] = question, system_prompt
        return OracleResult("ok", text="merged", model="claude-fable-5")

    monkeypatch.setattr(oracles.fable, "run", fake_fable)
    res = _run(oracles.run_synthesis("fable", "PANEL"))
    assert res.status == "ok" and res.key == "fable"
    # fable is the only backend with a system channel — the SYNTH prompt rides it
    assert seen["system"] == oracles.SYNTH_SYSTEM_PROMPT
    assert seen["question"] == "PANEL"
    assert res.telemetry is not None


def test_run_synthesis_codex_folds_prompt_into_message(monkeypatch):
    seen = {}

    async def fake_codex(question, context="", **kw):
        seen["question"] = question
        return OracleResult("ok", text="merged", model="gpt-5.6-sol")

    monkeypatch.setattr(oracles.codex, "run", fake_codex)
    res = _run(oracles.run_synthesis("codex", "PANEL"))
    assert res.status == "ok" and res.key == "codex"
    # no system channel on the codex bridge — the SYNTH prompt is folded in-message
    assert seen["question"].startswith(oracles.SYNTH_SYSTEM_PROMPT)
    assert seen["question"].endswith("PANEL")
    assert res.telemetry is not None


def test_run_synthesis_atlas_token(monkeypatch):
    seen = {}

    async def fake_atlas(model, question, context="", *, effort=None, timeout=None):
        seen["model"], seen["question"] = model, question
        return OracleResult("ok", text="merged", model=model)

    monkeypatch.setenv("ASK_FABLE_ATLAS_API_KEY", "k")
    monkeypatch.setattr(oracles.atlas, "run", fake_atlas)
    res = _run(oracles.run_synthesis("atlas:openai/gpt-5.6-sol", "PANEL"))
    assert res.status == "ok"
    assert seen["model"] == "openai/gpt-5.6-sol"
    assert seen["question"].startswith(oracles.SYNTH_SYSTEM_PROMPT)


def test_run_synthesis_uncached_dispatch_keeps_panelist_calls_clean(monkeypatch):
    # A panelist call (no system_prompt) must reach fable exactly as before —
    # the new kwargs default to no-ops.
    seen = {}

    async def fake_fable(question, context="", *, resume=None, system_prompt=None, on_think=None):
        seen["system"] = system_prompt
        return OracleResult("ok", text="ans", model="claude-fable-5")

    monkeypatch.setattr(oracles.fable, "run", fake_fable)
    res = _run(oracles.run("fable", "q about code"))
    assert res.status == "ok"
    assert seen["system"] is None
