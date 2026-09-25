"""ask_fable LM Studio oracle — config gating, catalog parsing, load/swap policy,
context handling, and response parsing."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

import ask_fable.lmstudio as lms
from ask_fable import oracles


def _run(coro):
    return asyncio.run(coro)


def _raw_model(key, *, type="llm", loaded=False, ctx=None, max_ctx=262144):
    m = {
        "key": key,
        "type": type,
        "publisher": "p",
        "display_name": key,
        "max_context_length": max_ctx,
        "size_bytes": 1000,
        "loaded_instances": [],
    }
    if loaded:
        m["loaded_instances"] = [{"id": key, "config": {"context_length": ctx or 32768}}]
    return m


class FakeLMStudio:
    """A stateful /api/v1/models + load/unload + chat fake."""

    def __init__(self, models, *, load_failures=0, load_error=None, chat=None):
        self.raw = {m["key"]: m for m in models}
        self.load_failures = load_failures
        self.load_error = load_error or "HTTP 400: insufficient memory to load model"
        self.calls = []
        self.loads = []
        self.unloads = []
        self.chat = chat if chat is not None else _chat_response("the answer")

    def request(self, path, *, method="GET", payload=None, timeout=None, empty_ok=False):
        self.calls.append((method, path, dict(payload or {})))
        if path == "/api/v1/models":
            models = [json.loads(json.dumps(m)) for m in self.raw.values()]
            return {"models": models}, None, 200
        if path == "/api/v1/models/load":
            self.loads.append(dict(payload))
            if self.load_failures > 0:
                self.load_failures -= 1
                return None, self.load_error, 400
            key = payload["model"]
            ctx = payload.get("context_length") or 4096
            self.raw[key]["loaded_instances"] = [
                {"id": key, "config": {"context_length": ctx}}
            ]
            return (
                {
                    "type": "llm",
                    "instance_id": key,
                    "status": "loaded",
                    "load_time_seconds": 1.5,
                    "load_config": {"context_length": ctx},
                },
                None,
                200,
            )
        if path == "/api/v1/models/unload":
            iid = payload.get("instance_id")
            self.unloads.append(iid)
            for m in self.raw.values():
                m["loaded_instances"] = [
                    i for i in m["loaded_instances"] if i.get("id") != iid
                ]
            return {"instance_id": iid}, None, 200
        if path == "/v1/chat/completions":
            return self.chat, None, 200
        raise AssertionError(f"unexpected request: {path}")


def _chat_response(
    text, *, model="m", prompt_tokens=100, completion_tokens=20, reasoning=None, finish="stop"
):
    msg = {"role": "assistant", "content": text}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return {
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# The real failure shape when a Vulkan KV allocation kills llama-server: the
# engine aborts and LM Studio wraps it in a generic model_load_failed with no
# memory wording anywhere (observed live on the 8 GB box, 2026-09-14).
_ENGINE_CRASH = (
    'HTTP 500: {"error": {"type": "model_load_failed", "message": "Failed to load LLM '
    "'m': Error: Engine protocol runtime llama-server for abc exited before becoming "
    'healthy. exitCode=null, signal=SIGABRT"}}'
)


def _install(monkeypatch, fake):
    monkeypatch.setattr(lms, "_request", fake.request)
    monkeypatch.setattr(lms.time, "sleep", lambda s: None)
    # Hermetic default: no control page, so the room check never touches the
    # network in tests — the host-identity probe is stubbed to "same machine"
    # and the GPU read to unavailable. Room-check tests override the GPU side.
    monkeypatch.setattr(lms.controlpage, "monitors", lambda *a, **k: True)
    monkeypatch.setattr(lms.controlpage, "gpu", lambda *a, **k: {"available": False})


def _clean_env(monkeypatch):
    for var in (
        "ASK_FABLE_LMSTUDIO_BASE_URL",
        "ASK_FABLE_LMSTUDIO_API_KEY",
        "ASK_FABLE_LMSTUDIO_MODEL",
        "ASK_FABLE_LMSTUDIO_SWAP",
        "ASK_FABLE_LMSTUDIO_CONTEXT",
        "ASK_FABLE_LMSTUDIO_MAX_TOKENS",
        "ASK_FABLE_LMSTUDIO_COUNCIL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_defaults_and_env(monkeypatch):
    _clean_env(monkeypatch)
    assert lms.base_url() == "http://lmstudio.example.com:1234"
    assert lms.api_key() is None
    assert lms.configured() is True
    assert lms.swap_policy() == "never"  # ask-first by default
    assert lms.context_default() == 32768
    assert lms.max_output_tokens() == 8192
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_BASE_URL", "http://box:1234/")
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_API_KEY", "tok")
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_MODEL", "qwen/qwen3.8-27b")
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "never")
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_CONTEXT", "65536")
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_MAX_TOKENS", "2048")
    assert lms.base_url() == "http://box:1234"  # trailing slash trimmed
    assert lms.api_key() == "tok"
    assert lms.default_model() == "qwen/qwen3.8-27b"
    assert lms.swap_policy() == "never"
    assert lms.context_default() == 65536
    assert lms.max_output_tokens() == 2048
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "auto")
    assert lms.swap_policy() == "auto"  # opt-in to automatic unload+retry


def _ceiling_config(monkeypatch, mapping):
    """Pin the tool-writable config to a ceilings mapping (conftest already
    isolated its path)."""
    monkeypatch.setattr(lms.config, "load", lambda: {"lmstudio_context_ceilings": mapping})


def test_context_ceiling_scopes_by_host_then_default(monkeypatch):
    _clean_env(monkeypatch)
    _ceiling_config(monkeypatch, {"smallbox": 8192, "default": 4096})
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_BASE_URL", "http://smallbox:1234")
    assert lms.context_ceiling() == 8192
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_BASE_URL", "http://elsewhere:1234")
    assert lms.context_ceiling() == 4096  # the fallback
    monkeypatch.setattr(lms.config, "load", lambda: {})
    assert lms.context_ceiling() == 0  # unlisted and no fallback: uncapped


def test_context_ceiling_wins_over_the_configured_window(monkeypatch):
    """ASK_FABLE_LMSTUDIO_CONTEXT=131072 must NOT reach a capped host — the
    engine there would abort on the KV allocation and dump a core."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_BASE_URL", "http://smallbox:1234")
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_CONTEXT", "131072")  # the request
    _ceiling_config(monkeypatch, {"smallbox": 8192})  # the policy
    fake = FakeLMStudio(
        [_raw_model("m", max_ctx=262144)],
        chat=_chat_response("capped", model="m"),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("m", "q"))
    assert res.status == "ok"
    assert fake.loads and fake.loads[0]["context_length"] == 8192
    assert res.meta["loaded_context_length"] == 8192


def test_prompt_over_the_ceiling_never_reaches_the_engine(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_BASE_URL", "http://smallbox:1234")
    _ceiling_config(monkeypatch, {"smallbox": 4096})
    fake = FakeLMStudio([_raw_model("m", max_ctx=262144)])
    _install(monkeypatch, fake)
    res = _run(lms.run("m", "x" * 16000))  # ~4600 est + system prompt > 4096
    assert res.status == "error" and res.kind == "context_too_small"
    assert "caps the context at 4096" in res.text
    assert fake.loads == []  # refused before any load attempt


def test_context_margin_reserved_pre_load(monkeypatch):
    # LD-2: the pre-load check must reserve the same _CONTEXT_MARGIN that run() subtracts,
    # or a prompt in the margin band passes pre-load, loads the model, then fails at run —
    # a wasted load. max_ctx here fits est+MIN_OUTPUT but not est+MIN_OUTPUT+MARGIN.
    _clean_env(monkeypatch)
    est = 3400
    max_ctx = est + lms._MIN_OUTPUT_TOKENS + lms._CONTEXT_MARGIN - 1  # in the band
    fake = FakeLMStudio([_raw_model("m", max_ctx=max_ctx)])
    _install(monkeypatch, fake)
    outcome = lms._ensure_loaded_locked("m", est)
    assert outcome.kind == "context_too_small"
    assert fake.loads == []  # rejected pre-load, nothing loaded


def test_chat_reverifies_resident_window_before_send(monkeypatch):
    # F4 fix-1: if the model is unloaded between the load decision and the send (e.g. a
    # concurrent unload from another ask_fable process), the chat must FAIL LOUDLY rather
    # than JIT-reload at LM Studio's app-default context and silently truncate the prompt.
    _clean_env(monkeypatch)
    fake = FakeLMStudio([_raw_model("m", loaded=True, ctx=32768, max_ctx=262144)])
    _install(monkeypatch, fake)
    # Simulate the unload landing right after this call took the in-flight refcount.
    real_add = lms._inflight_add

    def add_then_unload(key):
        added = real_add(key)
        for m in fake.raw.values():
            m["loaded_instances"] = []  # the model vanishes before the send
        return added

    monkeypatch.setattr(lms, "_inflight_add", add_then_unload)
    res = _run(lms.run("m", "q"))
    assert res.status == "error" and res.kind == "model_unavailable"
    assert not any(c[1] == "/v1/chat/completions" for c in fake.calls)  # nothing was sent


def test_a_resident_chat_never_waits_behind_a_load(monkeypatch):
    """A load holds _LOAD_LOCK in a worker thread for up to the load timeout. The
    in-flight refcount is taken ON THE EVENT LOOP, so it must not share that lock:
    it did, and a parallel call for an already-resident model froze the server."""
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [_raw_model("m", loaded=True, ctx=65536)], chat=_chat_response("fast", model="m")
    )
    _install(monkeypatch, fake)
    holding, release = threading.Event(), threading.Event()

    def long_load():
        with lms._LOAD_LOCK:
            holding.set()
            release.wait(5.0)  # bounds the pre-fix freeze

    loader = threading.Thread(target=long_load, daemon=True)
    loader.start()
    assert holding.wait(2.0)
    try:
        started = time.monotonic()
        res = _run(lms.run("m", "q"))
        elapsed = time.monotonic() - started
    finally:
        release.set()
        loader.join(5.0)
    assert res.status == "ok" and res.text == "fast"
    assert elapsed < 2.0  # answered while the load still held its lock
    assert lms._INFLIGHT == {}  # the refcount was released


def test_a_chat_cannot_start_on_a_model_mid_unload(monkeypatch):
    """With its own lock the refcount no longer serializes against an unload, so an
    unload CLAIMS its model: a chat arriving mid-unload is refused (retryably) rather
    than sent to a model on its way out, which LM Studio would JIT-reload at its
    ~4k default and silently truncate."""
    _clean_env(monkeypatch)
    fake = FakeLMStudio([_raw_model("m", loaded=True, ctx=65536)])
    _install(monkeypatch, fake)
    with lms._unload_claim("m") as claimed:
        assert claimed
        res = _run(lms.run("m", "q"))
    assert res.status == "error" and res.kind == "model_unavailable"
    assert not any(c[1] == "/v1/chat/completions" for c in fake.calls)
    assert lms._INFLIGHT == {} and lms._UNLOADING == set()
    # ...and a claim is refused while a chat is in flight.
    assert lms._inflight_add("m")
    try:
        with lms._unload_claim("m") as claimed:
            assert claimed is False
    finally:
        lms._inflight_done("m")


def test_catalog_parses_loaded_state(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [
            _raw_model("qwen3.8-27b-distill-q38", loaded=True, ctx=122172),
            _raw_model("openai/gpt-oss-120b"),
            _raw_model("embed/x", type="embeddings", loaded=True, ctx=2048),
        ]
    )
    _install(monkeypatch, fake)
    cat = lms.catalog()
    assert cat["ok"] is True and cat["endpoint"] == "http://lmstudio.example.com:1234"
    by_key = {m["key"]: m for m in cat["models"]}
    assert by_key["qwen3.8-27b-distill-q38"]["loaded"] is True
    assert by_key["qwen3.8-27b-distill-q38"]["loaded_context_length"] == 122172
    assert by_key["openai/gpt-oss-120b"]["loaded"] is False
    assert by_key["embed/x"]["type"] == "embeddings"
    assert lms.loaded_default() == "qwen3.8-27b-distill-q38"


def test_catalog_unreachable_is_not_an_exception(monkeypatch):
    _clean_env(monkeypatch)

    def boom(path, *, method="GET", payload=None, timeout=None):
        return None, "network error: refused", None

    monkeypatch.setattr(lms, "_request", boom)
    cat = lms.catalog()
    assert cat["ok"] is False and "unreachable" in cat["error"]
    assert lms.loaded_default() == ""


def test_run_already_loaded_never_loads_or_unloads(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [_raw_model("qwen3.8-27b-distill-q38", loaded=True, ctx=122172)],
        chat=_chat_response("the answer", model="qwen3.8-27b-distill-q38"),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("qwen3.8-27b-distill-q38", "How does dispatch work?"))
    assert res.status == "ok" and res.text == "the answer"
    assert res.meta["loaded_context_length"] == 122172 and res.meta["model_loaded"] is True
    assert fake.loads == [] and fake.unloads == []
    chat_calls = [c for c in fake.calls if c[1] == "/v1/chat/completions"]
    assert chat_calls and chat_calls[0][2]["model"] == "qwen3.8-27b-distill-q38"
    assert res.telemetry is not None and res.telemetry.usage_available is True


def test_run_loads_absent_model_without_evicting_resident(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("fresh", max_ctx=131072),
        ],
        chat=_chat_response("loaded answer", model="fresh"),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("fresh", "q"))
    assert res.status == "ok" and res.text == "loaded answer"
    assert fake.unloads == []  # the resident model was not bumped off
    assert fake.loads[0]["model"] == "fresh"
    assert fake.loads[0]["context_length"] == 32768  # explicit, never the 4k default
    assert "swapped" not in res.meta
    assert fake.raw["resident"]["loaded_instances"]  # still resident


def test_run_swaps_only_after_memory_failure(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "auto")
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("fresh", max_ctx=131072),
        ],
        # Fail the full context AND the smaller retry, so only unloading can fit it.
        load_failures=2,
        chat=_chat_response("swapped answer", model="fresh"),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("fresh", "q"))
    assert res.status == "ok" and res.text == "swapped answer"
    assert fake.unloads == ["resident"]  # unloaded only after both co-load attempts
    assert [p["model"] for p in fake.loads] == ["fresh", "fresh", "fresh"]
    assert fake.loads[0]["context_length"] == 32768
    assert fake.loads[1]["context_length"] < 32768  # the cheaper smaller-KV retry
    assert fake.loads[2]["context_length"] == 32768
    assert res.meta["swapped"] is True and res.meta["unloaded"] == ["resident"]
    assert res.meta["displaced"] == []


def test_run_memory_failure_with_swap_never_is_a_loud_error(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "never")
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("fresh", max_ctx=131072),
        ],
        # Both the full-context attempt and the always-on smaller-context retry
        # fail, so only unloading could fit it — and under "never" we ask first.
        load_failures=2,
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("fresh", "q"))
    assert res.status == "error" and res.kind == "load_failed"
    assert "lmstudio_swap=auto" in res.text and "resident" in res.text
    assert fake.unloads == []
    # The ask-first offer names what is in the way and the tool that frees it.
    offer = res.meta["unload_offer"]
    assert offer["ask_operator"] is True
    assert offer["tool"] == "unload_lms_model"
    assert [r["model"] for r in offer["resident"]] == ["resident"]


def test_run_engine_crash_retries_at_smaller_context_and_reports_it(monkeypatch):
    """A crash at the requested window: the smaller-context retry is
    non-destructive and runs in every policy, so it recovers here — and the
    reduction is reported, never silent."""
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [_raw_model("fresh", max_ctx=131072)],
        load_failures=1,
        load_error=_ENGINE_CRASH,
        chat=_chat_response("smaller answer", model="fresh"),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("fresh", "q"))
    assert res.status == "ok" and res.text == "smaller answer"
    assert len(fake.loads) == 2  # full window crashed, smaller window loaded
    assert fake.loads[0]["context_length"] == 32768
    smaller = fake.loads[1]["context_length"]
    assert smaller < 32768
    assert res.meta["context_reduced"] == {"from": 32768, "to": smaller}
    assert fake.unloads == []


def test_run_crash_probe_proves_memory_and_offers_without_unloading(monkeypatch):
    """The ambiguous crash (no memory wording) is only trusted after a
    floor-context load SUCCEEDS. Here it does, so the ask-first offer fires —
    and the probe instance itself is the only thing unloaded."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "never")
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("fresh", max_ctx=131072),
        ],
        load_failures=2,  # full window and the smaller retry both crash
        load_error=_ENGINE_CRASH,
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("fresh", "x" * 8000))  # `needed` sits above the floor
    assert res.status == "error" and res.kind == "load_failed"
    assert res.meta["crash_probe"] == "loaded_at_floor"
    assert [r["model"] for r in res.meta["unload_offer"]["resident"]] == ["resident"]
    assert "memory-bound" in res.text
    contexts = [p["context_length"] for p in fake.loads]
    assert contexts[0] == 32768 and contexts[1] < 32768
    assert contexts[2] == lms._FLOOR_CONTEXT  # the probe
    assert fake.unloads == ["fresh"]  # our own probe freed; the resident untouched
    assert fake.raw["resident"]["loaded_instances"]


def test_run_crash_that_fails_at_floor_is_never_taken_as_memory(monkeypatch):
    """A file that aborts at floor context too is not a memory shortfall: the
    raw error stands and, per the module's invariant, NOTHING is unloaded —
    even under swap=auto."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "auto")
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("fresh", max_ctx=131072),
        ],
        load_failures=3,  # full window, smaller retry, and the floor probe
        load_error=_ENGINE_CRASH,
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("fresh", "x" * 8000))
    assert res.status == "error" and res.kind == "load_failed"
    assert res.meta["crash_probe"] == "failed_at_floor"
    assert "broken" in res.text
    assert fake.unloads == []
    assert "unload_offer" not in res.meta
    assert fake.raw["resident"]["loaded_instances"]


def test_run_crash_probe_then_auto_swaps_one_resident(monkeypatch):
    """swap=auto with a proven-memory crash: the probe proves the file is fine,
    so the one-at-a-time unload may proceed and the crash error text must not
    stop the loop."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "auto")
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("fresh", max_ctx=131072),
        ],
        load_failures=2,
        load_error=_ENGINE_CRASH,
        chat=_chat_response("finally", model="fresh"),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("fresh", "x" * 8000))
    assert res.status == "ok" and res.text == "finally"
    assert fake.unloads == ["fresh", "resident"]  # probe first, then the resident
    assert res.meta["swapped"] is True and res.meta["unloaded"] == ["resident"]
    assert res.meta["crash_probe"] == "loaded_at_floor"


def test_run_reloads_loaded_model_when_context_too_small(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "auto")
    fake = FakeLMStudio(
        [_raw_model("small", loaded=True, ctx=4096)],
        chat=_chat_response("big ctx answer", model="small", prompt_tokens=6000),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("small", "x" * 20000))  # ~5700 est tokens + output > 4096
    assert res.status == "ok" and res.text == "big ctx answer"
    assert fake.unloads == ["small"]
    assert fake.loads[0]["model"] == "small" and fake.loads[0]["context_length"] > 4096
    assert res.meta["swapped"] is True and res.meta["unloaded"] == ["small"]


def test_run_small_context_with_swap_never_errors_before_loading(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "never")
    fake = FakeLMStudio([_raw_model("small", loaded=True, ctx=4096)])
    _install(monkeypatch, fake)
    res = _run(lms.run("small", "x" * 20000))
    assert res.status == "error" and res.kind == "context_too_small"
    assert "loaded at 4096" in res.text
    assert fake.unloads == [] and fake.loads == []
    offer = res.meta["unload_offer"]
    assert [r["model"] for r in offer["resident"]] == ["small"]


def test_run_caps_local_output_independently_of_the_global_cap(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_MAX_TOKENS", "65536")
    fake = FakeLMStudio(
        [_raw_model("m", loaded=True, ctx=131072)],
        chat=_chat_response("capped answer", model="m"),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("m", "q"))
    assert res.status == "ok"
    chat = [c for c in fake.calls if c[1] == "/v1/chat/completions"][0]
    assert chat[2]["max_tokens"] == 8192  # the local cap, not the global 65536


def test_run_room_check_offers_before_a_doomed_load(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("big", max_ctx=262144),
        ]
    )
    fake.raw["big"]["size_bytes"] = 30 * 1024**3  # 30 GiB: fits the GPU, not the free VRAM
    _install(monkeypatch, fake)
    monkeypatch.setattr(
        lms.controlpage,
        "gpu",
        lambda *a, **k: {
            "available": True,
            "vram_free_mib": 20480,
            "vram_total_mib": 48935,
            "util_pct": 0,
        },
    )
    res = _run(lms.run("big", "q"))
    assert res.status == "error" and res.kind == "load_failed"
    assert fake.loads == [] and fake.unloads == []  # no doomed load attempt
    offer = res.meta["unload_offer"]
    assert offer["gpu"]["vram_free_mib"] == 20480
    assert [r["model"] for r in offer["resident"]] == ["resident"]
    assert "not enough free VRAM" in res.text
    assert "blocking: resident" in res.text  # names what to free, not just "a resident"


def test_run_room_check_too_large_is_refused_without_an_unload_offer(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("huge", max_ctx=262144),
        ]
    )
    fake.raw["huge"]["size_bytes"] = 60 * 1024**3  # 60 GiB > 47.8 GiB total
    _install(monkeypatch, fake)
    monkeypatch.setattr(
        lms.controlpage,
        "gpu",
        lambda *a, **k: {
            "available": True,
            "vram_free_mib": 24346,
            "vram_total_mib": 48935,
            "util_pct": 0,
        },
    )
    res = _run(lms.run("huge", "q"))
    assert res.status == "error" and res.kind == "model_too_large"
    assert "unload_offer" not in res.meta  # unloading cannot help — no lie offered
    assert res.meta["gpu"]["vram_total_mib"] == 48935
    assert "cannot make it fit" in res.text
    assert fake.loads == [] and fake.unloads == []


def test_room_verdict_boundaries():
    gpu = {"available": True, "vram_free_mib": 20480, "vram_total_mib": 48935}
    assert lms.room_verdict(4 * 1024**3, gpu) == "fits"
    assert lms.room_verdict(30 * 1024**3, gpu) == "needs_room"
    assert lms.room_verdict(60 * 1024**3, gpu) == "too_large"
    assert lms.room_verdict(4 * 1024**3, {"available": False}) == "unknown"
    assert lms.room_verdict(None, gpu) == "unknown"


def test_run_room_check_skipped_when_the_page_is_another_machine(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [_raw_model("big", max_ctx=262144)],
        chat=_chat_response("loaded anyway", model="big"),
    )
    fake.raw["big"]["size_bytes"] = 30 * 1024**3  # would read as needs_room on lmstudio.example.com's numbers
    _install(monkeypatch, fake)
    monkeypatch.setattr(lms.controlpage, "monitors", lambda *a, **k: False)

    def _boom(snapshot=None, *a, **k):
        if snapshot is None:  # the fetching call; gpu({}) is a local normalization
            raise AssertionError("a page for another machine must not be fetched")
        return {"available": False}

    monkeypatch.setattr(lms.controlpage, "gpu", _boom)
    res = _run(lms.run("big", "q"))
    # Degrades to "unknown" and attempts the load — llama.cpp gives the real verdict.
    assert res.status == "ok" and res.text == "loaded anyway"
    assert fake.loads and fake.loads[0]["model"] == "big"


def test_run_room_check_lets_a_fitting_load_proceed(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("small", max_ctx=131072),
        ],
        chat=_chat_response("room ok", model="small"),
    )
    fake.raw["small"]["size_bytes"] = 4 * 1024**3  # 4 GiB
    _install(monkeypatch, fake)
    monkeypatch.setattr(
        lms.controlpage,
        "gpu",
        lambda *a, **k: {
            "available": True,
            "vram_free_mib": 20480,
            "vram_total_mib": 48935,
            "util_pct": 0,
        },
    )
    res = _run(lms.run("small", "q"))
    assert res.status == "ok" and res.text == "room ok"
    assert fake.loads and fake.loads[0]["model"] == "small"


def test_run_unknown_model_suggests(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio([_raw_model("qwen3.8-27b-distill-q38")])
    _install(monkeypatch, fake)
    res = _run(lms.run("qwen3.8-27b", "q"))
    assert res.status == "error" and res.kind == "model_not_found"
    assert "qwen3.8-27b-distill-q38" in res.text and "available" in res.text


def test_run_truncation_is_flagged_and_never_reported_as_clean(monkeypatch):
    _clean_env(monkeypatch)
    # A prompt pinned at the window ceiling: reported prompt + completion ≈ ctx.
    fake = FakeLMStudio(
        [_raw_model("big", loaded=True, ctx=8192)],
        chat=_chat_response("truncated answer", model="big", prompt_tokens=8170),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("big", "x" * 20000))
    assert res.status == "ok" and res.kind == "truncated"
    assert res.text.startswith("[ask_fable warning:")
    assert "truncated answer" in res.text


def test_run_output_cap_is_flagged_truncated_not_clean(monkeypatch):
    # finish_reason "length": the answer was cut off by the output cap. It is still
    # returned, but flagged so no cache layer pins it as complete.
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [_raw_model("m", loaded=True, ctx=65536)],
        chat=_chat_response("half an ans", model="m", finish="length"),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("m", "q"))
    assert res.status == "ok" and res.kind == "truncated"
    assert res.text == "half an ans" and res.meta.get("partial") is True


def test_run_budget_spent_reasoning_is_not_a_health_error(monkeypatch):
    # All output tokens spent thinking, no answer: the request's budget, not a sick
    # backend — `budget_exhausted` (non-health) instead of `sdk_error`.
    from ask_fable import health

    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [_raw_model("m", loaded=True, ctx=65536)],
        chat=_chat_response("", model="m", reasoning="thinking...", finish="length"),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("m", "q"))
    assert res.status == "error" and res.kind == "budget_exhausted"
    assert "spent all" in res.text
    assert "budget_exhausted" in health._NON_HEALTH_KINDS


def test_run_parses_reasoning_and_inline_think(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [_raw_model("m", loaded=True, ctx=65536)],
        chat=_chat_response("clean", model="m", reasoning="because"),
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("m", "q"))
    assert res.text == "clean" and res.thinking == "because"

    assert lms._split_thinking("<think>why</think>final") == ("final", "why")
    assert lms._split_thinking("no thoughts") == ("no thoughts", "")


def test_run_chat_http_error(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio([_raw_model("m", loaded=True, ctx=65536)])
    fake.chat = None

    def chat_fail(path, *, method="GET", payload=None, timeout=None):
        fake.calls.append((method, path, dict(payload or {})))
        if path == "/v1/chat/completions":
            return None, "HTTP 500: boom", 500
        return fake.request(path, method=method, payload=payload, timeout=timeout)

    _install(monkeypatch, fake)
    monkeypatch.setattr(lms, "_request", chat_fail)
    res = _run(lms.run("m", "q"))
    assert res.status == "error" and "LM Studio request failed" in res.text
    assert res.telemetry is not None and res.telemetry.http_status == 500


def test_run_failed_swap_restores_the_resident_model(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "auto")
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("fresh", max_ctx=131072),
        ],
        load_failures=3,  # full, smaller retry, and post-unload all fail
    )
    _install(monkeypatch, fake)
    res = _run(lms.run("fresh", "q"))
    assert res.status == "error" and res.kind == "load_failed"
    assert fake.unloads == ["resident"]
    assert "restored" in res.text and "resident" in res.text
    # The resident was reloaded at its previous context.
    assert fake.raw["resident"]["loaded_instances"]
    restore = [p for p in fake.loads if p["model"] == "resident"]
    assert restore and restore[0]["context_length"] == 65536


def _two_residents_and_fresh(monkeypatch):
    """swap=auto with residents `a` then `b`; `fresh` fails its full load, its
    smaller retry, and the retry after `a` is unloaded — so the swap reaches `b`."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "auto")
    fake = FakeLMStudio(
        [
            _raw_model("a", loaded=True, ctx=8192),
            _raw_model("b", loaded=True, ctx=16384),
            _raw_model("fresh", max_ctx=131072),
        ],
        load_failures=3,
    )
    _install(monkeypatch, fake)
    return fake


def _restored_at(fake, key):
    return [p["context_length"] for p in fake.loads if p["model"] == key]


def test_swap_that_fails_a_later_unload_restores_the_earlier_one(monkeypatch):
    fake = _two_residents_and_fresh(monkeypatch)
    real = fake.request

    def b_will_not_unload(path, **kw):
        if path == "/api/v1/models/unload" and kw["payload"]["instance_id"] == "b":
            fake.unloads.append("b")
            return None, "HTTP 500: unload exploded", 500
        return real(path, **kw)

    monkeypatch.setattr(lms, "_request", b_will_not_unload)
    res = _run(lms.run("fresh", "q"))
    # It used to unpack the (key, ctx) pairs as triples: a ValueError surfaced as
    # sdk_error and `a` was never put back.
    assert res.status == "error" and res.kind == "unload_failed"
    assert fake.unloads == ["a", "b"]
    assert fake.raw["a"]["loaded_instances"] and _restored_at(fake, "a") == [8192]
    assert "could not unload b" in res.text
    assert "a had already been unloaded" in res.text and "were restored" in res.text


def test_swap_blocked_by_a_busy_resident_restores_the_earlier_one(monkeypatch):
    fake = _two_residents_and_fresh(monkeypatch)
    lms._INFLIGHT["b"] = 1
    try:
        res = _run(lms.run("fresh", "q"))
    finally:
        lms._INFLIGHT.pop("b", None)
    assert res.status == "error" and res.kind == "busy"
    assert fake.unloads == ["a"]  # never `b`, which is mid-chat
    # `a` was already unloaded when `b` turned out busy: it must be put back and named.
    assert fake.raw["a"]["loaded_instances"] and _restored_at(fake, "a") == [8192]
    assert "a had already been unloaded" in res.text and "were restored" in res.text


@pytest.mark.parametrize("load_failures, restored", [(1, True), (2, False)])
def test_failed_reload_reports_the_restore_truthfully(monkeypatch, load_failures, restored):
    """_restore returns the keys it could NOT put back; the message read it
    inverted and said "could not be restored" exactly when the restore worked."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "auto")
    fake = FakeLMStudio([_raw_model("small", loaded=True, ctx=4096)], load_failures=load_failures)
    _install(monkeypatch, fake)
    res = _run(lms.run("small", "x" * 20000))  # needs more than the resident 4096
    assert res.status == "error" and res.kind == "load_failed"
    assert bool(fake.raw["small"]["loaded_instances"]) is restored
    assert ("could not be restored" in res.text) is not restored
    assert ("restored at its previous 4096-token context" in res.text) is restored


def test_run_swap_refuses_to_unload_a_busy_model(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_SWAP", "auto")
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("fresh", max_ctx=131072),
        ],
        load_failures=2,
    )
    _install(monkeypatch, fake)
    lms._INFLIGHT["resident"] = 1
    try:
        res = _run(lms.run("fresh", "q"))
    finally:
        lms._INFLIGHT.pop("resident", None)
    assert res.status == "error" and res.kind == "busy"
    assert "answering another ask_lms call" in res.text
    assert fake.unloads == []  # never pulled out from under the in-flight chat


def test_truncated_unknown_window_uses_estimate_fallback():
    assert lms._truncated(0, 1000, 400, 0) is True
    assert lms._truncated(0, 1000, 600, 0) is False
    assert lms._truncated(8192, 1000, 8170, 30) is True
    assert lms._truncated(8192, 1000, 100, 30) is False


def test_request_survives_incomplete_read(monkeypatch):
    import http.client

    class _BoomOpener:
        def open(self, req, timeout=None):
            raise http.client.IncompleteRead(b"partial")

    _clean_env(monkeypatch)
    monkeypatch.setattr(lms, "_get_opener", lambda: _BoomOpener())
    obj, err, status = lms._request("/api/v1/models", timeout=1.0)
    assert obj is None and status is None and "network error" in err


def test_unload_waits_for_confirmation_and_reports_freed_space(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [
            _raw_model("resident", loaded=True, ctx=65536),
            _raw_model("other"),
        ]
    )
    _install(monkeypatch, fake)
    out = lms.unload("resident")
    assert out["status"] == "ok" and out["unload_confirmed"] is True
    assert out["unloaded"] == ["resident"] and out["resident"] == []
    assert out["freed_bytes"] == 1000
    assert fake.unloads == ["resident"]


def test_unload_not_loaded_is_a_noop(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio([_raw_model("cold")])
    _install(monkeypatch, fake)
    out = lms.unload("cold")
    assert out["status"] == "ok" and out["unloaded"] == []
    assert fake.unloads == []


def test_unload_refuses_while_a_chat_is_in_flight(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio([_raw_model("resident", loaded=True, ctx=65536)])
    _install(monkeypatch, fake)
    lms._INFLIGHT["resident"] = 1
    try:
        out = lms.unload("resident")
    finally:
        lms._INFLIGHT.pop("resident", None)
    assert out["status"] == "error" and out["kind"] == "busy"
    assert fake.unloads == []


def test_council_models_precedence(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setattr(lms.config, "get_list", lambda key: None)
    assert lms.council_models() == list(lms.DEFAULT_LMSTUDIO_COUNCIL)
    monkeypatch.setenv("ASK_FABLE_LMSTUDIO_COUNCIL", "a, b , lmstudio:c")
    assert lms.council_models() == ["a", "b", "c"]  # prefix stripped, order kept
    monkeypatch.setattr(lms.config, "get_list", lambda key: ["config/model"])
    assert lms.council_models() == ["config/model"]  # config file wins


def test_resident_models_lists_only_loaded_chat_models(monkeypatch):
    _clean_env(monkeypatch)
    fake = FakeLMStudio(
        [
            _raw_model("loaded-llm", loaded=True, ctx=4096),
            _raw_model("cold-llm"),
            _raw_model("loaded-embed", type="embeddings", loaded=True, ctx=2048),
        ]
    )
    _install(monkeypatch, fake)
    assert lms.resident_models() == ["loaded-llm"]


def test_oracle_token_recognition_and_casing():
    assert oracles.lmstudio_model("lmstudio:qwen/qwen3.8-27b") == "qwen/qwen3.8-27b"
    assert oracles.lmstudio_model("lmstudio:") is None
    assert oracles.label("lmstudio:Meta/Muse-Glimmer") == "Meta/Muse-Glimmer"
    recognized, unknown = oracles.resolve(["lmstudio:Meta/Muse-Glimmer", "nope"])
    assert recognized == ["lmstudio:Meta/Muse-Glimmer"]  # casing preserved, order kept
    assert unknown == ["nope"]
    ordered, unknown = oracles.resolve_ordered(["lmstudio:m", "fable"])
    assert ordered == ["lmstudio:m", "fable"] and unknown == []
