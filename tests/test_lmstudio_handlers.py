"""ask_fable LM Studio tool handlers — ask_lms and list_lms_models. Guard and
bridge stubbed."""

from __future__ import annotations

import asyncio

import pytest

import ask_fable.server as server
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _quiet_and_no_audit(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


def _run(coro):
    return asyncio.run(coro)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def test_ask_lms_ok(monkeypatch):
    _allow(monkeypatch)
    seen = {}

    async def fake_run(model, question, context=""):
        seen["model"] = model
        return OracleResult("ok", text="local answer", model=model, thinking="th")

    monkeypatch.setattr(server.lmstudio, "run", fake_run)
    out = _run(
        server._handle_lms(
            {"question": "How is the router wired?", "model": "qwen/qwen3.8-27b"}
        )
    )
    assert out["status"] == "ok" and out["answer"] == "local answer"
    assert out["model"] == "qwen/qwen3.8-27b"
    assert seen["model"] == "qwen/qwen3.8-27b"


def test_ask_lms_merges_bridge_meta_without_clobbering(monkeypatch):
    _allow(monkeypatch)

    async def fake_run(model, question, context=""):
        return OracleResult(
            "ok",
            text="a",
            model=model,
            meta={
                "loaded_context_length": 32768,
                "swapped": True,
                "load_confirmed": True,
                "status": "nope",  # must not overwrite the real status
            },
        )

    monkeypatch.setattr(server.lmstudio, "run", fake_run)
    out = _run(server._handle_lms({"question": "trace the load path", "model": "m"}))
    assert out["status"] == "ok"
    assert out["loaded_context_length"] == 32768
    assert out["swapped"] is True and out["load_confirmed"] is True


def test_ask_lms_truncated_result_is_not_cached(monkeypatch):
    _allow(monkeypatch)
    puts = []
    monkeypatch.setattr(server.cache, "put", lambda k, v: puts.append(k))

    async def fake_run(model, question, context=""):
        return OracleResult("ok", text="[ask_fable warning: truncated]\nans", model=model,
                            kind="truncated")

    monkeypatch.setattr(server.lmstudio, "run", fake_run)
    out = _run(server._handle_lms({"question": "a unique truncated probe", "model": "m"}))
    assert out["status"] == "ok"
    assert puts == []


def test_ask_lms_uses_configured_default(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.lmstudio, "default_model", lambda: "qwen/qwen3.8-27b")

    async def fake_run(model, question, context=""):
        return OracleResult("ok", text="a", model=model)

    monkeypatch.setattr(server.lmstudio, "run", fake_run)
    out = _run(server._handle_lms({"question": "explain the module layout"}))
    assert out["model"] == "qwen/qwen3.8-27b"


def test_ask_lms_falls_back_to_single_resident_model(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.lmstudio, "default_model", lambda: "")
    monkeypatch.setattr(server.lmstudio, "loaded_default", lambda: "resident")

    async def fake_run(model, question, context=""):
        return OracleResult("ok", text="a", model=model)

    monkeypatch.setattr(server.lmstudio, "run", fake_run)
    out = _run(server._handle_lms({"question": "which model answers this?"}))
    assert out["model"] == "resident"


def test_ask_lms_no_model_anywhere_is_bad_args(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.lmstudio, "default_model", lambda: "")
    monkeypatch.setattr(server.lmstudio, "loaded_default", lambda: "")
    out = _run(server._handle_lms({"question": "hello there"}))
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert "list_lms_models" in out["detail"]


def test_ask_lms_guard_denied(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (False, "prohibited_x"))
    out = _run(server._handle_lms({"question": "blocked", "model": "m"}))
    assert out["status"] == "refused" and out["stage"] == "guard"


def test_ask_lms_error_payload_carries_the_unload_offer(monkeypatch):
    _allow(monkeypatch)

    async def fake_run(model, question, context=""):
        return OracleResult(
            "error",
            kind="load_failed",
            text="could not load m: insufficient memory",
            model=model,
            meta={
                "unload_offer": {
                    "tool": "unload_lms_model",
                    "ask_operator": True,
                    "resident": [{"model": "resident", "size_bytes": 123}],
                }
            },
        )

    monkeypatch.setattr(server.lmstudio, "run", fake_run)
    out = _run(server._handle_lms({"question": "needs a fresh local model", "model": "m"}))
    assert out["status"] == "error" and out["kind"] == "load_failed"
    assert out["unload_offer"]["ask_operator"] is True
    assert out["unload_offer"]["resident"][0]["model"] == "resident"


def test_unload_lms_model_handler(monkeypatch):
    monkeypatch.setattr(
        server.lmstudio,
        "unload",
        lambda model: {
            "status": "ok",
            "model": model,
            "unloaded": [model],
            "freed_bytes": 16547400032,
            "unload_confirmed": True,
            "resident": [],
        },
    )
    out = _run(server._handle_unload_lms({"model": "qwen3.8-27b-distill-q38"}))
    assert out["status"] == "ok" and out["unload_confirmed"] is True
    assert out["freed_bytes"] == 16547400032


def test_unload_lms_model_requires_a_model():
    out = _run(server._handle_unload_lms({}))
    assert out["status"] == "error" and out["kind"] == "bad_args"


def test_host_status_reports_gpu_and_host(monkeypatch):
    monkeypatch.setattr(
        server.controlpage,
        "status",
        lambda: {
            "gpu": {
                "available": True,
                "util_pct": 12,
                "vram_used_mib": 24589,
                "vram_total_mib": 48935,
                "temp_c": 43,
            },
            "services": {"lmstudio": "active"},
            "lmstudio": {"loaded_models": ["m"], "models": ["m"]},
        },
    )
    out = _run(server._handle_host_status({}))
    assert out["status"] == "ok"
    assert out["gpu"]["util_pct"] == 12
    assert out["gpu"]["vram_free_mib"] == 48935 - 24589
    assert out["lmstudio"]["loaded_models"] == ["m"]


def test_host_status_unreachable_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(server.controlpage, "status", lambda: None)
    out = _run(server._handle_host_status({}))
    assert out["status"] == "error" and out["kind"] == "unreachable"
    assert "ASK_FABLE_CONTROL_URL" in out["detail"]


def test_ask_lms_council_sequential_and_synthesizes(monkeypatch):
    _allow(monkeypatch)
    keys, unloads = [], []
    monkeypatch.setattr(server.lmstudio, "resident_models", lambda: [])
    monkeypatch.setattr(
        server.lmstudio, "unload", lambda m: unloads.append(m) or {"status": "ok"}
    )

    async def fake_oracle_run(key, question, context=""):
        keys.append(key)
        return OracleResult("ok", key=key, text=f"{key} ans", model=server.oracles.label(key))

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="MERGED local panel")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    out = _run(
        server._handle_lms_council(
            {"question": "How does the router wire modules?", "models": ["a", "b"]}
        )
    )
    assert out["status"] == "ok" and out["answer"] == "MERGED local panel"
    # Sequential, in requested order, each key keeping the lmstudio: attribution.
    assert keys == ["lmstudio:a", "lmstudio:b"]
    # Each member WE loaded is freed after its turn (none pre-existed here).
    assert unloads == ["a", "b"]
    assert set(out["sources"]) == {"lmstudio:a", "lmstudio:b"}


def test_ask_lms_council_leaves_pre_existing_members_resident(monkeypatch):
    _allow(monkeypatch)
    unloads = []
    monkeypatch.setattr(server.lmstudio, "resident_models", lambda: ["qwen3.8-27b-distill-q38"])
    monkeypatch.setattr(
        server.lmstudio, "unload", lambda m: unloads.append(m) or {"status": "ok"}
    )

    async def fake_oracle_run(key, question, context=""):
        return OracleResult("ok", key=key, text=f"{key} ans", model=server.oracles.label(key))

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="MERGED")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    out = _run(
        server._handle_lms_council(
            {
                "question": "How does routing work here?",
                "models": ["qwen3.8-27b-distill-q38", "fresh"],
            }
        )
    )
    assert out["status"] == "ok"
    assert unloads == ["fresh"]  # the pre-existing model was not touched


def test_ask_lms_council_keeps_residents_named_by_another_spelling(monkeypatch):
    """The cleanup compared each member TOKEN to the resident keys by exact string,
    but run/unload match fuzzily (case, @variant) — so 'qwen/qwen3.6-35b-a3b' was
    answered by a pre-resident 'qwen/qwen3.6-35b-a3b@q4_k_m' and then unloaded it.
    Only a model the token does not resolve to among the residents is ours to free."""
    _allow(monkeypatch)
    unloads = []
    monkeypatch.setattr(
        server.lmstudio,
        "resident_models",
        lambda: ["qwen/qwen3.6-35b-a3b@q4_k_m", "Org/Coder-30B"],
    )
    monkeypatch.setattr(
        server.lmstudio, "unload", lambda m: unloads.append(m) or {"status": "ok"}
    )

    async def fake_oracle_run(key, question, context=""):
        return OracleResult("ok", key=key, text=f"{key} ans", model=server.oracles.label(key))

    async def fake_synthesis(key, prompt, **kw):
        return OracleResult("ok", key=key, text="MERGED", model=server.oracles.label(key))

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.oracles, "run_synthesis", fake_synthesis)
    monkeypatch.setattr(server.oracles, "available", lambda k: True)
    out = _run(
        server._handle_lms_council(
            {
                "question": "How does routing work here?",
                "models": [
                    "qwen/qwen3.6-35b-a3b",  # bare -> the resident @q4_k_m variant
                    "org/coder-30b",  # case differs -> the resident Org/Coder-30B
                    "qwen/qwen3.6-35b-a3b@q8_0",  # a different variant: loaded for us
                ],
                # a local synthesizer resolving to a resident is left alone too
                "synthesizer": "lmstudio:QWEN/qwen3.6-35b-a3b",
            }
        )
    )
    assert out["status"] == "ok" and out["answer"] == "MERGED"
    assert unloads == ["qwen/qwen3.6-35b-a3b@q8_0"]


def test_ask_lms_council_defaults_to_configured_set(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.lmstudio, "council_models", lambda: ["qwen/qwen3.5-9b"])
    monkeypatch.setattr(server.lmstudio, "resident_models", lambda: [])
    monkeypatch.setattr(server.lmstudio, "unload", lambda m: {"status": "ok"})
    keys = []

    async def fake_oracle_run(key, question, context=""):
        keys.append(key)
        return OracleResult("ok", key=key, text=f"{key} ans", model=server.oracles.label(key))

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        return OracleResult("ok", text="MERGED")

    monkeypatch.setattr(server.oracles, "run", fake_oracle_run)
    monkeypatch.setattr(server.fable, "run", fake_fable)
    out = _run(server._handle_lms_council({"question": "How does routing work here?"}))
    assert out["status"] == "ok"
    assert keys == ["lmstudio:qwen/qwen3.5-9b"]


def test_ask_lms_council_no_models(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.lmstudio, "council_models", lambda: [])
    out = _run(server._handle_lms_council({"question": "trace the router please", "models": []}))
    assert out["status"] == "error" and out["kind"] == "no_models"


def test_list_lms_models_reports_loaded_and_available(monkeypatch):
    monkeypatch.setattr(
        server.lmstudio,
        "catalog",
        lambda: {
            "ok": True,
            "endpoint": "http://lmstudio.example.com:1234",
            "models": [
                {
                    "key": "qwen3.8-27b-distill-q38",
                    "type": "llm",
                    "loaded": True,
                    "instance_id": "qwen3.8-27b-distill-q38",
                    "loaded_context_length": 122172,
                    "max_context_length": 262144,
                    "size_bytes": 16547400032,
                },
                {
                    "key": "openai/gpt-oss-120b",
                    "type": "llm",
                    "loaded": False,
                    "instance_id": None,
                    "loaded_context_length": None,
                    "max_context_length": 131072,
                    "size_bytes": 60000000000,
                },
                {
                    "key": "mid-model",
                    "type": "llm",
                    "loaded": False,
                    "instance_id": None,
                    "loaded_context_length": None,
                    "max_context_length": 131072,
                    "size_bytes": 30 * 1024**3,
                },
                {
                    "key": "small-model",
                    "type": "llm",
                    "loaded": False,
                    "instance_id": None,
                    "loaded_context_length": None,
                    "max_context_length": 131072,
                    "size_bytes": 4 * 1024**3,
                },
                {
                    "key": "embed/x",
                    "type": "embeddings",
                    "loaded": True,
                    "instance_id": "embed/x",
                    "loaded_context_length": 2048,
                    "max_context_length": 2048,
                },
            ],
            "error": "",
        },
    )
    monkeypatch.setattr(
        server.controlpage,
        "gpu",
        lambda *a, **k: {
            "available": True,
            "vram_free_mib": 20480,
            "vram_total_mib": 48935,
            "util_pct": 0,
        },
    )
    out = _run(server._handle_list_lms({"refresh": True}))
    assert out["status"] == "ok" and out["reachable"] is True
    assert [m["model"] for m in out["loaded"]] == ["qwen3.8-27b-distill-q38"]
    assert out["loaded"][0]["context_length"] == 122172
    assert out["loaded"][0]["size_bytes"] == 16547400032
    assert out["loaded_bytes"] == 16547400032
    assert out["available"] == ["openai/gpt-oss-120b", "mid-model", "small-model"]
    assert out["fits_now"] == ["small-model"]
    assert out["needs_unload"] == ["mid-model"]
    assert out["too_large"] == ["openai/gpt-oss-120b"]
    assert out["gpu"]["vram_free_mib"] == 20480
    assert out["swap_policy"] in ("auto", "never")
    assert out["output_cap"] >= 256


def test_list_lms_models_no_refresh_skips_network(monkeypatch):
    def _boom():
        raise AssertionError("catalog() must not be called when refresh=false")

    monkeypatch.setattr(server.lmstudio, "catalog", _boom)
    out = _run(server._handle_list_lms({"refresh": False}))
    assert out["status"] == "ok" and out["loaded"] == [] and out["reachable"] is False
