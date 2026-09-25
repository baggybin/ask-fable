"""Reach a model on an LM Studio server for one reasoning turn.

``ask_lms`` is the single-model tool; ``lmstudio:<model>`` is also accepted as
an explicit oracle token in ``ask_chain`` / ``ask_council`` (deliberately NOT
part of any tier preset — name it if you want it). Chat rides the
OpenAI-compatible ``POST /v1/chat/completions`` dialect; the non-stock half is
LM Studio's own model lifecycle, and that is most of this module.

Policy (the operator's requirements):
1. A model already loaded is used as-is — never unloaded or reloaded just to
   change its context while it still fits.
2. A model not loaded is loaded EXPLICITLY (``POST /api/v1/models/load``). An
   explicit load is not a JIT load: it does not trigger LM Studio's Auto-Evict
   (which unloads previously JIT-loaded models when a new model is JIT-loaded)
   and the instance gets no idle TTL. Resident models are therefore not bumped
   off to make room.
3. ``context_length`` is ALWAYS sent explicitly on load. Omitting it lets LM
   Studio fall back to its app-wide default (historically 4096), which SILENTLY
   truncates long ask_fable prompts — the worst failure class, because the
   answer still comes back confidently. Default 32768, raised to fit the actual
   prompt when needed, capped to the model's max; ``lmstudio_context`` /
   ``ASK_FABLE_LMSTUDIO_CONTEXT`` pins it.
4. When it genuinely does not fit — LM Studio reports a memory failure on load,
   or the resident instance is loaded at a context smaller than the prompt
   needs — the DEFAULT behavior is ask-first: the call fails with a structured
   ``unload_offer`` naming the resident model(s), their sizes and the
   ``unload_lms_model`` tool, and the operator decides. ``lmstudio_swap=auto``
   opts back into automatic management: unload blocking instances ONE AT A TIME
   (never a model with a chat in flight) and retry; WAIT until each unload and
   the load are confirmed in the catalog; on failure, best-effort RESTORE
   whatever was unloaded at its previous context.
   A smaller-context retry runs first in every policy (it touches no resident),
   and on this Vulkan stack a failed KV allocation often surfaces as an engine
   CRASH instead of a memory message. A crash is never taken as memory on its
   word: it licenses only non-destructive retries plus a floor-context probe
   (``_probe_crash_memory``), and only a probe that LOADS proves the failure
   memory-bound — a file that also fails at floor context is returned as-is
   with nothing unloaded.
   Nothing here ever unloads a model outside those two paths.

Concurrency: loads are serialized by a process-wide ``threading.Lock`` AND an
``fcntl`` file lock (ask_fable runs one server process per MCP client session,
so the thread lock alone would let two sessions create two instances of the
same model), re-checked inside the lock. The common already-loaded path is
checked WITHOUT the lock first so a big load cannot queue an answer that needs
no load. While a chat is in flight the model is recorded in an in-process
refcount; the swap path refuses to unload a busy model rather than pulling it
out from under a generation. The refcount has its own small lock (the event
loop takes it, so it must never wait behind a load), and an unload claims its
model under that lock, so no chat can start on a model mid-unload. Every HTTP
call is stdlib urllib (no new dependency) run via ``asyncio.to_thread``; the
operator's proxy environment is deliberately ignored for this bridge, because a
LAN host must not be routed through a proxy.
"""

from __future__ import annotations

import asyncio
import difflib
import fcntl
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import _paths, config, controlpage
from .openai_compat import finish_reason
from .oracle_common import (
    CAPPED_STOP_REASONS,
    RETRYABLE_HTTP_STATUSES,
    OracleResult,
    TransientRetryTimeout,
    call_with_transient_retry,
    compose,
    http_error_detail,
    max_tokens,
)
from .prompts import ORACLE_SYSTEM_PROMPT  # same scope contract for every oracle
from .provider_telemetry import ProviderTelemetry, ProviderUsage, token_count, usage_if_available

DEFAULT_LMSTUDIO_BASE_URL = "http://lmstudio.example.com:1234"
DEFAULT_LMSTUDIO_CONTEXT = 32768
# Default panel for `ask_lms_council`: the fast, strong, practical models from
# the 2026-09-13 sweep (a single GPU serves them sequentially, so this stays
# lean). Override per call with `models`, or persistently with config
# `lmstudio_council` / ASK_FABLE_LMSTUDIO_COUNCIL.
DEFAULT_LMSTUDIO_COUNCIL = (
    "qwen/qwen3.6-35b-a3b",  # ~60 tok/s MoE, strongest local answerer
    "qwen/qwen3.8-27b",  # ~45 tok/s
    "qwen/qwen3.5-9b",  # ~44 tok/s small
    "zai-org/glm-4.6v-flash",  # ~39 tok/s
    "google/gemma-4-31b-qat",  # ~29 tok/s
)
# Local generation is slow (~55 tok/s on a 27B), so an uncapped answer can run
# for many minutes and blow a chain's client timeout. 8192 tokens is ~2.5 min
# worst case and still a generous answer; raise per machine.
DEFAULT_LMSTUDIO_MAX_TOKENS = 8192
DEFAULT_CHAT_TIMEOUT = 600.0
DEFAULT_LOAD_TIMEOUT = 600.0
DEFAULT_UNLOAD_WAIT = 180.0

# A completion needs room for a real answer: if the context cannot fit the
# prompt plus this much output, fail loudly rather than ask for a stub.
_MIN_OUTPUT_TOKENS = 512
# Floor context for the crash probe: the smallest window a load is attempted at
# to tell a readable file from an unreadable one. Weights never fail here (they
# spill to CPU), so a readable file loads at floor size even under VRAM pressure.
_FLOOR_CONTEXT = 2048
# Headroom between prompt + output and the context ceiling. llama.cpp can shrink
# the prompt so that prompt + n_predict fits n_ctx, so sitting exactly on the
# edge makes a large max_tokens CAUSE truncation.
_CONTEXT_MARGIN = 256
# Prompt-token estimate. Conservative (over-estimates tokens for typical code by
# ~15%), because under-estimating is what lets a prompt slip past the context
# check and get silently truncated by LM Studio.
_EST_CHARS_PER_TOKEN = 3.5
# Strong memory-failure vocabulary. A load failure that does NOT match one of
# these is returned as-is — we do not unload a resident model for a corrupt
# file or a bad variant name. Deliberately phrase-level: a bare "oom" matches
# "room" and a bare "alloc" matches a corrupt-tensor error.
_MEMORY_HINTS = (
    "not enough memory",
    "insufficient memory",
    "out of memory",
    "vram",
    "failed to allocate",
    "memory allocation",
)
_CHAT_MODEL_TYPES = ("llm", "vlm")
# Free-VRAM headroom required by the control-page room check, on top of the
# model's GGUF weight size, before a co-load is attempted.
_ROOM_HEADROOM_MIB = 2048

# One lock per process: LM Studio's load endpoint happily creates a second
# instance of the same model, so the read-check-load sequence must be atomic.
_LOAD_LOCK = threading.Lock()
# key -> chats currently in flight. The swap path will not unload a model
# answering another call. Guarded by its OWN lock, held only for a dict update:
# `run` takes it on the event loop, while _LOAD_LOCK is held by a worker thread
# for a whole load (up to the load timeout) — waiting on that froze the server.
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, int] = {}
# Keys an unload is under way for (see `_unload_claim`); no chat may start on one.
_UNLOADING: set[str] = set()
# Chat POSTs currently open in a worker thread, by key. Guarded by _INFLIGHT_LOCK.
_GENERATING: dict[str, int] = {}

_THINK_RE = re.compile(r"^\s*<think(?:ing)?>(.*?)</think(?:ing)?>\s*", re.DOTALL | re.IGNORECASE)

_opener: urllib.request.OpenerDirector | None = None


def base_url() -> str:
    """The LM Studio server root (``ASK_FABLE_LMSTUDIO_BASE_URL``)."""
    raw = (os.environ.get("ASK_FABLE_LMSTUDIO_BASE_URL") or "").strip() or DEFAULT_LMSTUDIO_BASE_URL
    return raw.rstrip("/")


def api_key() -> str | None:
    """Optional bearer token (LM Studio 0.4 can require API tokens)."""
    return (os.environ.get("ASK_FABLE_LMSTUDIO_API_KEY") or "").strip() or None


def configured() -> bool:
    """True when a server is configured; reachability is decided at call time
    (a LAN host may simply be down, which is a per-call network error)."""
    return bool(base_url())


def default_model() -> str:
    """The model ``ask_lms`` uses when none is passed: config file
    (``lmstudio_model``) → ``ASK_FABLE_LMSTUDIO_MODEL`` → empty (the handler
    then falls back to the single resident model)."""
    return config.get_str("lmstudio_model") or (
        os.environ.get("ASK_FABLE_LMSTUDIO_MODEL") or ""
    ).strip()


def dedupe_models(parts: list[str]) -> list[str]:
    """Strip an optional ``lmstudio:`` prefix off each id and de-dupe,
    order-preserving (casing kept — LM Studio keys are case-sensitive)."""
    out: list[str] = []
    prefix = "lmstudio:"
    for p in parts:
        m = p[len(prefix) :] if p.lower().startswith(prefix) else p
        m = m.strip()
        if m and m not in out:
            out.append(m)
    return out


def council_models() -> list[str]:
    """Default panel for ``ask_lms_council`` / a ``models`` omission.

    Precedence: config file (``lmstudio_council``) → ``ASK_FABLE_LMSTUDIO_COUNCIL``
    (comma- or whitespace-separated) → ``DEFAULT_LMSTUDIO_COUNCIL``. Bare model
    keys, no prefix."""
    from_config = config.get_list("lmstudio_council")
    if from_config:
        return dedupe_models(from_config)
    raw = os.environ.get("ASK_FABLE_LMSTUDIO_COUNCIL")
    if raw is None or not raw.strip():
        return list(DEFAULT_LMSTUDIO_COUNCIL)
    return dedupe_models([p.strip() for p in raw.replace(",", " ").split()])


def resident_models() -> list[str]:
    """Keys of the resident chat models — used by ``ask_lms_council`` to tell
    models it loaded (and may free) from ones that were already there."""
    models = _models_entries() or []
    return [
        str(m["key"])
        for m in models
        if m.get("loaded") and m.get("type") in _CHAT_MODEL_TYPES
    ]


def resident_key(model: str, resident: Iterable[str]) -> str | None:
    """The key in ``resident`` (a ``resident_models`` snapshot) that ``model``
    names under the SAME match ``run`` and ``unload`` apply — exact, then
    case-insensitive, then ``@variant``-stripped — or None when it names none.

    A caller freeing only the models it loaded must compare this, not the token's
    spelling: ``qwen/x`` is served by (and would unload) a resident
    ``qwen/x@q4_k_m``, so a spelling check evicts a model that was there first."""
    entry = _find_model([{"key": k, "loaded": True} for k in resident], model)
    return str(entry["key"]) if entry else None


def swap_policy() -> str:
    """``never`` (default, ask-first) or ``auto``: may a load that does not fit
    unload the resident model(s) after waiting for confirmation? Under ``never``
    the blocked load returns an ``unload_offer`` naming what to free and the
    operator decides; ``auto`` restores the earlier no-prompt behavior."""
    raw = (
        config.get_str("lmstudio_swap") or os.environ.get("ASK_FABLE_LMSTUDIO_SWAP") or "never"
    ).strip().lower()
    return "auto" if raw in ("auto", "on", "true", "yes", "1") else "never"


def context_default() -> int:
    """Default context window requested on load (``lmstudio_context`` /
    ``ASK_FABLE_LMSTUDIO_CONTEXT``, default 32768)."""
    raw = config.get_str("lmstudio_context") or os.environ.get("ASK_FABLE_LMSTUDIO_CONTEXT")
    try:
        return max(1024, int(str(raw)))
    except (TypeError, ValueError):
        return DEFAULT_LMSTUDIO_CONTEXT


def context_ceiling() -> int:
    """Per-host context ceiling from config ``lmstudio_context_ceilings``.

    0 means no ceiling. A small box (an 8 GB eGPU over Thunderbolt) ABORTS its
    engine on a KV allocation it cannot make — LM Studio surfaces that only as
    a generic ``model_load_failed`` crash, after a coredump — so an over-large
    window must never even be requested there. Entries are keyed by the
    ``ASK_FABLE_LMSTUDIO_BASE_URL`` hostname, with ``"default"`` as the
    fallback; a ceiling always wins over ``lmstudio_context``."""
    raw = config.load().get("lmstudio_context_ceilings")
    if not isinstance(raw, dict):
        return 0
    host = (urllib.parse.urlparse(base_url()).hostname or "").lower()
    for key in (host, "default"):
        value = raw.get(key)
        try:
            if value is not None:
                return max(1024, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def max_output_tokens() -> int:
    """Output cap for one local generation (``lmstudio_max_tokens`` /
    ``ASK_FABLE_LMSTUDIO_MAX_TOKENS``, default 8192) — separate from the global
    ``ASK_FABLE_MAX_TOKENS`` because a slow local model must not run for many
    minutes; ask_fable's cloud oracles keep the global cap."""
    raw = config.get_str("lmstudio_max_tokens") or os.environ.get(
        "ASK_FABLE_LMSTUDIO_MAX_TOKENS"
    )
    try:
        return max(256, int(str(raw)))
    except (TypeError, ValueError):
        return DEFAULT_LMSTUDIO_MAX_TOKENS


def chat_timeout() -> float:
    """Wall-clock budget for one completion (``ASK_FABLE_LMSTUDIO_TIMEOUT``)."""
    return _env_float("ASK_FABLE_LMSTUDIO_TIMEOUT", DEFAULT_CHAT_TIMEOUT)


def load_timeout() -> float:
    """Wall-clock budget for one load, which can take minutes for a big model
    (``ASK_FABLE_LMSTUDIO_LOAD_TIMEOUT``)."""
    return _env_float("ASK_FABLE_LMSTUDIO_LOAD_TIMEOUT", DEFAULT_LOAD_TIMEOUT)


def unload_wait() -> float:
    """How long to wait for an unload to be confirmed gone before giving up
    (``ASK_FABLE_LMSTUDIO_UNLOAD_WAIT``). A 16 GB model can take >30 s."""
    return _env_float("ASK_FABLE_LMSTUDIO_UNLOAD_WAIT", DEFAULT_UNLOAD_WAIT)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


@contextmanager
def _file_lock() -> Iterator[None]:
    """Cross-PROCESS load serialization via ``fcntl.flock``.

    ask_fable runs one server process per MCP client session, so the thread
    lock alone would let two sessions load the same unloaded model twice (LM
    Studio creates a second instance). flock is released by the kernel if the
    process dies, so a crash cannot strand the lock. Best-effort: an FS that
    cannot lock degrades to the in-process lock rather than failing the call.
    """
    lock_path = _paths.xdg_state_dir() / "ask_fable" / "lmstudio.lock"
    if not _paths.ensure_dir_secure(lock_path.parent):
        yield
        return
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _get_opener() -> urllib.request.OpenerDirector:
    """A module-wide opener with proxies disabled: ``lmstudio.example.com`` is a LAN host and
    must never be routed through an operator's ``http_proxy``."""
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _opener


def _headers() -> dict:
    headers = {"accept": "application/json", "content-type": "application/json"}
    key = api_key()
    if key:
        headers["authorization"] = f"Bearer {key}"
    return headers


def _request(
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout: float,
    empty_ok: bool = False,
) -> tuple[dict | None, str | None, int | None]:
    """One JSON request. Returns ``(obj, error, http_status)`` and never raises
    for transport failures. ``empty_ok`` treats a 2xx empty body as ``{}`` (the
    unload endpoint answers with no body on some builds)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{base_url()}{path}", data=data, method=method, headers=_headers()
    )
    try:
        with _get_opener().open(req, timeout=timeout) as resp:
            body = resp.read()
            if not body.strip():
                if empty_ok:
                    return {}, None, resp.status
                return None, "empty response from LM Studio", resp.status
            try:
                obj = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None, "unparseable response from LM Studio", resp.status
            return (obj if isinstance(obj, dict) else None), None, resp.status
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return None, f"HTTP {e.code}: {detail or e.reason}", e.code
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        return None, f"network error: {reason}", None


def _int_or_none(value: object) -> int | None:
    return value if type(value) is int else None


def catalog(timeout: float = 6.0) -> dict:
    """Discover the models LM Studio knows about (best-effort, never raises).

    Returns ``{"ok": bool, "endpoint": str, "models": [...], "error": str}``;
    each model carries ``key``, ``type``, ``publisher``, ``display_name``,
    ``max_context_length``, ``size_bytes``, ``loaded``, ``instance_id`` and
    ``loaded_context_length`` when resident."""
    obj, err, status = _request("/api/v1/models", timeout=timeout)
    if obj is None:
        if status in (401, 403):
            why = "LM Studio requires an API key — set ASK_FABLE_LMSTUDIO_API_KEY"
        else:
            why = f"LM Studio unreachable at {base_url()}: {err or 'no response'}"
        return {"ok": False, "endpoint": base_url(), "models": [], "error": why}
    models: list[dict] = []
    for m in obj.get("models") or []:
        if not isinstance(m, dict):
            continue
        key = str(m.get("key") or "").strip()
        if not key:
            continue
        instances = [i for i in (m.get("loaded_instances") or []) if isinstance(i, dict)]
        inst = instances[0] if instances else None
        cfg = (inst or {}).get("config") or {}
        models.append(
            {
                "key": key,
                "type": str(m.get("type") or ""),
                "publisher": str(m.get("publisher") or ""),
                "display_name": str(m.get("display_name") or ""),
                "max_context_length": _int_or_none(m.get("max_context_length")),
                "size_bytes": _int_or_none(m.get("size_bytes")),
                "loaded": inst is not None,
                "instance_id": str((inst or {}).get("id") or "") or None,
                "loaded_context_length": _int_or_none(cfg.get("context_length")),
            }
        )
    return {"ok": True, "endpoint": base_url(), "models": models, "error": ""}


def loaded_default() -> str:
    """The key of the single resident chat model, or "" when zero/multiple are
    loaded — an unambiguous zero-config default, never a guessed one."""
    cat = catalog()
    loaded = [
        m["key"]
        for m in cat.get("models") or []
        if m.get("loaded") and m.get("type") in _CHAT_MODEL_TYPES
    ]
    return loaded[0] if len(loaded) == 1 else ""


def _models_entries(timeout: float = 6.0) -> list[dict] | None:
    cat = catalog(timeout)
    return cat.get("models") if cat.get("ok") else None


def _find_model(models: list[dict], name: str) -> dict | None:
    """Exact key, then case-insensitive key, then key with any ``@variant``
    suffix stripped (preferring a loaded variant) — LM Studio keys are otherwise
    case-sensitive."""
    want = (name or "").strip()
    if not want:
        return None
    low = want.lower()
    for m in models:
        if m.get("key") == want:
            return m
    for m in models:
        if str(m.get("key", "")).lower() == low:
            return m
    variants = [
        m for m in models if str(m.get("key", "")).split("@", 1)[0].lower() == low
    ]
    variants.sort(key=lambda m: not m.get("loaded"))
    return variants[0] if variants else None


def _suggest(models: list[dict], name: str) -> list[str]:
    keys = [str(m["key"]) for m in models if m.get("type") in _CHAT_MODEL_TYPES]
    return difflib.get_close_matches(name, keys, n=3, cutoff=0.4)


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text or "") / _EST_CHARS_PER_TOKEN))


def _needed_context(est_prompt_tokens: int) -> int:
    """The minimum usable window: prompt + a real answer + headroom."""
    return est_prompt_tokens + _MIN_OUTPUT_TOKENS + _CONTEXT_MARGIN


def _desired_context(est_prompt_tokens: int, max_context: int) -> int:
    want = max(context_default(), _needed_context(est_prompt_tokens))
    if max_context > 0:
        want = min(want, max_context)
    ceiling = context_ceiling()
    if ceiling:
        want = min(want, ceiling)
    return want


def _looks_memory(error: str) -> bool:
    low = (error or "").lower()
    return any(h in low for h in _MEMORY_HINTS)


def _looks_engine_crash(error: str) -> bool:
    """The engine died before becoming healthy — on this Vulkan stack that is
    how a failed KV allocation surfaces: the backend throws, llama-server
    aborts, and LM Studio returns a generic ``model_load_failed`` with no
    memory wording anywhere. A corrupt file aborts the same way, so this is
    deliberately NOT a memory classifier — it licenses only non-destructive
    retries and the floor-context probe that tells the two apart
    (see ``_probe_crash_memory``)."""
    low = (error or "").lower()
    return "model_load_failed" in low and "exited before becoming healthy" in low


def _unload_offer(reason: str, residents: list[dict]) -> dict:
    """The operator-facing choice a blocked load returns under the ask-first
    policy: what is in the way, how much it occupies, and the tool to free it."""
    return {
        "tool": "unload_lms_model",
        "ask_operator": True,
        "reason": reason,
        "resident": residents,
        "alternative": "set lmstudio_swap=auto to unload automatically and retry",
    }


def _resident_info(models: list[dict], snapshot: list[tuple[str, str, int]]) -> list[dict]:
    sizes = {str(m.get("key")): m.get("size_bytes") for m in models}
    return [
        {"model": key, "size_bytes": sizes.get(key), "loaded_context_length": ctx}
        for key, _inst, ctx in snapshot
    ]


def room_verdict(size_bytes: object, gpu: dict) -> str:
    """Free-VRAM verdict for one model against a control-page GPU block.

    One of ``fits`` / ``needs_room`` / ``too_large`` / ``unknown``:
      - ``fits``: free VRAM covers the weights + headroom now
      - ``needs_room``: it fits the GPU's TOTAL but not the free VRAM now, so
        unloading a resident model can genuinely help (that is when the caller
        may offer an unload)
      - ``too_large``: it exceeds TOTAL VRAM — unloading cannot help, so an
        unload offer would be a lie
      - ``unknown``: page/GPU/size unavailable — caller just attempts the load
    """
    free = gpu.get("vram_free_mib")
    total = gpu.get("vram_total_mib")
    # A NEGATIVE size used to floor-divide to -1 and read as "fits"; a numeric STRING
    # from the control page reached the comparison and raised TypeError. Both are
    # "the page told us nothing usable", which is what `unknown` means.
    size_mib = size_bytes // (1024 * 1024) if type(size_bytes) is int and size_bytes > 0 else 0
    if not isinstance(free, (int, float)) or not isinstance(total, (int, float)):
        return "unknown"
    if not gpu.get("available") or not size_mib:
        return "unknown"
    need = size_mib + _ROOM_HEADROOM_MIB
    if need > total:
        return "too_large"
    return "fits" if free >= need else "needs_room"


def _room_verdict(size_bytes: object) -> tuple[str, dict]:
    """Best-effort classification via the control page. Only used on the
    ask-first path, where it can replace a doomed multi-minute load attempt
    with an instant, honest answer.

    The page is consulted ONLY when it monitors the machine the LM Studio
    server actually runs on: it is a fixed URL (lmstudio.example.com), so a bridge pointed
    at another box would otherwise do its VRAM math on the primary GPU — a
    false "fits" is exactly the overconfidence this check exists to prevent.
    A mismatch (or unresolvable hosts) degrades to "unknown": attempt the load
    and let llama.cpp return the real verdict."""
    lms_host = (urllib.parse.urlparse(base_url()).hostname or "").lower()
    if not lms_host or not controlpage.monitors(lms_host):
        return "unknown", controlpage.gpu({})
    gpu_now = controlpage.gpu()
    return room_verdict(size_bytes, gpu_now), gpu_now


@contextmanager
def _generating(key: str) -> Iterator[None]:
    """Count a chat POST that is OPEN on ``key``, from inside the worker thread.

    `_INFLIGHT` is released when the awaiting coroutine unwinds — but the POST
    runs in a thread `asyncio.to_thread` cannot cancel, so a council that hit its
    timeout dropped the count while the model was still generating, and the
    cleanup unload right after it pulled the model out from under a live
    generation (the one thing `unload`'s docstring promises never happens). This
    counter is raised and lowered by the THREAD, so it outlives the cancellation.
    """
    with _INFLIGHT_LOCK:
        _GENERATING[key] = _GENERATING.get(key, 0) + 1
    try:
        yield
    finally:
        with _INFLIGHT_LOCK:
            left = _GENERATING.get(key, 0) - 1
            if left > 0:
                _GENERATING[key] = left
            else:
                _GENERATING.pop(key, None)


def _inflight_add(key: str) -> bool:
    """Count a chat in flight on ``key``. False — nothing counted, do not send —
    while an unload of ``key`` is under way."""
    with _INFLIGHT_LOCK:
        if key in _UNLOADING:
            return False
        _INFLIGHT[key] = _INFLIGHT.get(key, 0) + 1
        return True


def _inflight_done(key: str) -> None:
    with _INFLIGHT_LOCK:
        n = _INFLIGHT.get(key, 0) - 1
        if n > 0:
            _INFLIGHT[key] = n
        else:
            _INFLIGHT.pop(key, None)


@contextmanager
def _unload_claim(key: str) -> Iterator[bool]:
    """Claim ``key`` for an unload. Yields False when a chat is in flight on it
    (claim not taken — do not unload); otherwise True, and no chat can start on
    it until the block exits. The busy check and the claim are ONE step under
    _INFLIGHT_LOCK, so a chat cannot slip in between "not busy" and the unload it
    licensed; one that arrives after the block finds the model gone at its
    pre-send re-check. Every caller holds _LOAD_LOCK, so claims never overlap."""
    with _INFLIGHT_LOCK:
        # Both counters matter: _INFLIGHT is the awaiting caller, _GENERATING the
        # POST still open in a thread after that caller was cancelled.
        claimed = not _INFLIGHT.get(key) and not _GENERATING.get(key)
        if claimed:
            _UNLOADING.add(key)
    try:
        yield claimed
    finally:
        if claimed:
            with _INFLIGHT_LOCK:
                _UNLOADING.discard(key)


@dataclass
class _LoadOutcome:
    kind: str = ""
    detail: str = ""
    model: str = ""
    context_length: int = 0
    swapped: bool = False
    loaded_now: bool = False
    unloaded: list[str] = field(default_factory=list)
    displaced: list[str] = field(default_factory=list)
    displaced_unknown: bool = False
    load_time_s: float | None = None
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.kind


def _load_instance(
    model: str, context_length: int, timeout: float
) -> tuple[dict | None, str | None, int | None]:
    """Explicit, non-JIT load. Returns ``(info, error, http_status)``."""
    payload: dict = {"model": model, "echo_load_config": True}
    if context_length:
        payload["context_length"] = context_length
    obj, err, status = _request(
        "/api/v1/models/load", method="POST", payload=payload, timeout=timeout
    )
    if err:
        return None, err, status
    cfg = obj.get("load_config") if isinstance(obj, dict) else None
    info = {
        "context_length": _int_or_none((cfg or {}).get("context_length")),
        "load_time_s": (
            round(float(obj["load_time_seconds"]), 1)
            if isinstance(obj, dict) and isinstance(obj.get("load_time_seconds"), (int, float))
            else None
        ),
    }
    return info, None, status


def _unload_instance(instance_id: str, timeout: float) -> str | None:
    _obj, err, _status = _request(
        "/api/v1/models/unload",
        method="POST",
        payload={"instance_id": instance_id},
        timeout=timeout,
        empty_ok=True,
    )
    return err


def _resident_snapshot(models: list[dict], *, exclude: str = "") -> list[tuple[str, str, int]]:
    """``(key, instance_id, loaded_context_length)`` for every resident chat
    model except ``exclude``."""
    out: list[tuple[str, str, int]] = []
    for m in models:
        if m.get("key") == exclude or not m.get("loaded"):
            continue
        if m.get("type") not in _CHAT_MODEL_TYPES:
            continue
        out.append(
            (
                str(m["key"]),
                str(m.get("instance_id") or m["key"]),
                int(m.get("loaded_context_length") or 0),
            )
        )
    return out


def _wait_until(predicate, wait_s: float, interval: float = 2.0) -> bool:
    """Poll ``predicate`` (a blocking catalog check) until it returns True or
    ``wait_s`` elapses. This is the 'wait and confirm' half of the swap path —
    an unload that has been requested is not an unload that has happened."""
    deadline = time.monotonic() + wait_s
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _is_loaded(models: list[dict], key: str) -> bool:
    entry = _find_model(models, key)
    return bool(entry and entry.get("loaded"))


def _wait_loaded(key: str, wait_s: float) -> bool:
    return _wait_until(
        lambda: (entries := _models_entries()) is not None and _is_loaded(entries, key),
        wait_s,
        interval=1.0,
    )


def _confirmed_context(key: str) -> int:
    """Re-read the catalog for the instance's ACTUAL context length. 0 when the
    build does not report it (then the caller must not pretend it knows)."""
    entries = _models_entries()
    if entries is None:
        return 0
    entry = _find_model(entries, key)
    return int((entry or {}).get("loaded_context_length") or 0)


def _unload_and_wait(key: str, instance_id: str) -> str | None:
    """Unload one instance and confirm it is gone from the catalog. Returns an
    error string (do NOT proceed to load) or None."""
    err = _unload_instance(instance_id, timeout=min(load_timeout(), 120.0))
    if err:
        return f"could not unload {key}: {err}"
    gone = _wait_until(
        lambda: (
            (entries := _models_entries()) is not None
            and not any(
                str(m.get("instance_id") or "") == instance_id and m.get("loaded")
                for m in entries
            )
        ),
        unload_wait(),
    )
    if not gone:
        return (
            f"{key} did not unload within {unload_wait():.0f}s "
            "(still resident; raise ASK_FABLE_LMSTUDIO_UNLOAD_WAIT or unload it manually)"
        )
    return None


def _probe_crash_memory(key: str) -> tuple[bool, str]:
    """Floor-context probe for an ambiguous engine crash: load ``key`` at
    ``_FLOOR_CONTEXT`` and free it again. Returns ``(proven, detail)``.

    - ``(True, "")``: the file LOADED, so it is readable and the earlier crash
      at a larger window was memory-bound. The probe instance is unloaded
      before returning, so the caller's next move starts from a clean state.
    - ``(False, why)``: the probe failed to load (the file itself looks broken)
      or could not be freed again (state unknown) — either way the unload path
      must not run.

    This is the evidence gate the module docstring's invariant requires: a
    message pattern can never prove memory, only a load can."""
    _info, err, _status = _load_instance(key, _FLOOR_CONTEXT, load_timeout())
    if err:
        return False, err
    entry = _find_model(_models_entries() or [], key)
    inst = str((entry or {}).get("instance_id") or key)
    # L14: claim it first. The probe's own unload used to run unguarded, so a chat
    # that started on the probe instance in between had it removed mid-generation.
    with _unload_claim(key) as claimed:
        if not claimed:
            return False, f"{key} is answering a chat — cannot run the memory probe now"
        uerr = _unload_and_wait(key, inst)
    if uerr:
        return False, f"{key} loaded at floor context but could not be freed again: {uerr}"
    return True, ""


def _restore(unloaded: list[tuple[str, int]]) -> list[str]:
    """Best-effort reload of models the swap path unloaded, at their previous
    context. Returns the keys that could NOT be restored."""
    failed: list[str] = []
    for key, ctx in unloaded:
        _info, err, _status = _load_instance(key, ctx, load_timeout())
        if err:
            failed.append(key)
    return failed


def _restore_clause(unloaded: list[tuple[str, int]]) -> str:
    """Restore, newest first, the ``(key, context)`` residents a swap unloaded
    before giving up, and return the clause that tells the caller how it went."""
    failed = _restore(unloaded[::-1])
    if failed:
        return f"; could not restore {', '.join(failed)}"
    return "; the unloaded model(s) were restored"


def _abandon_swap(
    unloaded: list[tuple[str, int]], *, kind: str, detail: str, model: str
) -> _LoadOutcome:
    """Stop the swap part-way. Residents it already unloaded are put back and
    named — a give-up that left them off (or said nothing of them) cost the
    operator a model they never agreed to lose."""
    if unloaded:
        names = ", ".join(k for k, _c in unloaded)
        detail += f"; {names} had already been unloaded for this swap" + _restore_clause(unloaded)
    return _LoadOutcome(kind=kind, detail=detail, model=model, unloaded=[k for k, _c in unloaded])


def _resident_list(models: list[dict]) -> list[dict]:
    return [
        {
            "model": m["key"],
            "size_bytes": m.get("size_bytes"),
            "loaded_context_length": m.get("loaded_context_length"),
        }
        for m in models
        if m.get("loaded") and m.get("type") in _CHAT_MODEL_TYPES
    ]


def unload(model: str) -> dict:
    """Operator-requested unload of ONE model from the LM Studio server.

    Blocking; callers wrap it in a thread. Refuses while the model has an
    ask_lms chat in flight (never pull a model out from under a generation),
    waits for the unload to be CONFIRMED gone, and reports the bytes freed and
    what remains resident. Unloading an already-unloaded model is a no-op ok.
    Never called implicitly — ask_lms under the default ask-first policy returns
    an offer instead."""
    model = (model or "").strip()
    if not model:
        return {"status": "error", "kind": "bad_args", "detail": "`model` is required"}
    with _LOAD_LOCK, _file_lock():
        models = _models_entries()
        if models is None:
            return {
                "status": "error",
                "kind": "network_error",
                "detail": f"LM Studio unreachable at {base_url()} (is the server running?)",
            }
        entry = _find_model(models, model)
        if entry is None:
            suggestions = _suggest(models, model)
            detail = f"no LM Studio model matching {model!r}"
            if suggestions:
                detail += f" — did you mean {', '.join(suggestions)}?"
            return {"status": "error", "kind": "model_not_found", "detail": detail}
        key = str(entry["key"])
        if not entry.get("loaded"):
            return {
                "status": "ok",
                "model": key,
                "unloaded": [],
                "freed_bytes": 0,
                "detail": f"{key} is not loaded",
                "resident": _resident_list(models),
            }
        with _unload_claim(key) as claimed:
            if not claimed:
                return {
                    "status": "error",
                    "kind": "busy",
                    "detail": f"{key} is answering another ask_lms call; retry shortly",
                    "model": key,
                }
            err = _unload_and_wait(key, str(entry.get("instance_id") or key))
        if err:
            return {"status": "error", "kind": "unload_failed", "detail": err, "model": key}
        after = _models_entries()
        return {
            "status": "ok",
            "model": key,
            "unloaded": [key],
            "freed_bytes": entry.get("size_bytes"),
            "unload_confirmed": True,
            "resident": _resident_list(after or []),
        }


def _not_found_outcome(models: list[dict], model: str) -> _LoadOutcome:
    suggestions = _suggest(models, model)
    available = [str(m["key"]) for m in models if m.get("type") in _CHAT_MODEL_TYPES]
    detail = f"no LM Studio model matching {model!r}"
    if suggestions:
        detail += f" — did you mean {', '.join(suggestions)}?"
    detail += f" (available: {', '.join(available) or 'none'})"
    return _LoadOutcome(kind="model_not_found", detail=detail, model=model)


def _quick_loaded(model: str, est: int) -> _LoadOutcome | None:
    """Lock-free fast path: an already-resident model that fits the prompt.
    Returns None when a load/reload (and thus the locks) is needed; error
    outcomes for the cases no load could fix."""
    models = _models_entries()
    if models is None:
        return _LoadOutcome(
            kind="network_error",
            detail=f"LM Studio unreachable at {base_url()} (is the server running and "
            "ASK_FABLE_LMSTUDIO_BASE_URL right?)",
            model=model,
        )
    entry = _find_model(models, model)
    if entry is None:
        return _not_found_outcome(models, model)
    if entry.get("type") not in _CHAT_MODEL_TYPES:
        return _LoadOutcome(
            kind="model_not_found",
            detail=f"{entry.get('key')} is a {entry.get('type') or 'non-chat'} model, not a chat model",
            model=str(entry.get("key")),
        )
    max_ctx = int(entry.get("max_context_length") or 0)
    if max_ctx and _needed_context(est) > max_ctx:
        return _LoadOutcome(
            kind="context_too_small",
            detail=f"prompt is ~{est} tokens but {entry.get('key')} maxes out at "
            f"{max_ctx}; split the question or pack less context",
            model=str(entry["key"]),
        )
    if entry.get("loaded"):
        loaded_ctx = int(entry.get("loaded_context_length") or 0)
        if not loaded_ctx:
            # The build did not report the loaded window: proceed, but do not
            # fabricate one (context_length=0 → context_unverified in the result).
            return _LoadOutcome(model=str(entry["key"]), context_length=0)
        if loaded_ctx >= _needed_context(est):
            return _LoadOutcome(model=str(entry["key"]), context_length=loaded_ctx)
    return None


def _ensure_loaded_locked(model: str, est: int) -> _LoadOutcome:
    """The load/reload decision, under both locks. See the module docstring."""
    models = _models_entries()
    if models is None:
        return _LoadOutcome(
            kind="network_error",
            detail=f"LM Studio unreachable at {base_url()} (is the server running and "
            "ASK_FABLE_LMSTUDIO_BASE_URL right?)",
            model=model,
        )
    entry = _find_model(models, model)
    if entry is None:
        return _not_found_outcome(models, model)
    key = str(entry["key"])
    if entry.get("type") not in _CHAT_MODEL_TYPES:
        return _LoadOutcome(
            kind="model_not_found",
            detail=f"{key} is a {entry.get('type') or 'non-chat'} model, not a chat model",
            model=key,
        )
    max_ctx = int(entry.get("max_context_length") or 0)
    if max_ctx and _needed_context(est) > max_ctx:
        return _LoadOutcome(
            kind="context_too_small",
            detail=f"prompt is ~{est} tokens but {key} maxes out at {max_ctx}; "
            "split the question or pack less context",
            model=key,
        )
    ceiling = context_ceiling()
    if ceiling and _needed_context(est) > ceiling:
        return _LoadOutcome(
            kind="context_too_small",
            detail=f"prompt is ~{est} tokens but this host caps the context at {ceiling} "
            "(lmstudio_context_ceilings); split the question, pack less context, or run "
            "this model where a larger window is allowed",
            model=key,
        )
    want = _desired_context(est, max_ctx)

    if entry.get("loaded"):
        loaded_ctx = int(entry.get("loaded_context_length") or 0)
        if not loaded_ctx or loaded_ctx >= _needed_context(est):
            return _LoadOutcome(model=key, context_length=loaded_ctx)
        if swap_policy() != "auto":
            return _LoadOutcome(
                kind="context_too_small",
                detail=f"{key} is loaded at {loaded_ctx} tokens but the prompt needs "
                f"~{est}+{_MIN_OUTPUT_TOKENS}; unload it and re-ask (it will load at the "
                f"needed window), or set lmstudio_context={want} "
                "(ASK_FABLE_LMSTUDIO_CONTEXT) and lmstudio_swap=auto to reload it",
                model=key,
                meta={
                    "unload_offer": _unload_offer(
                        "the resident instance's context is too small for this prompt",
                        [
                            {
                                "model": key,
                                "size_bytes": entry.get("size_bytes"),
                                "loaded_context_length": loaded_ctx,
                            }
                        ],
                    )
                },
            )
        # Same model, bigger context: unload + reload. It is the requested
        # model, so this bumps nothing else off.
        inst = str(entry.get("instance_id") or key)
        with _unload_claim(key) as claimed:
            if not claimed:
                return _LoadOutcome(
                    kind="busy",
                    detail=f"{key} is answering another ask_lms call; retry shortly",
                    model=key,
                )
            err = _unload_and_wait(key, inst)
        if err:
            return _LoadOutcome(kind="unload_failed", detail=err, model=key)
        info, err, _status = _load_instance(key, want, load_timeout())
        if err:
            # _restore returns the keys it could NOT put back.
            not_restored = _restore([(key, loaded_ctx)])
            detail = f"could not reload {key} at {want}: {err}"
            if not_restored:
                detail += " (it was unloaded and could not be restored)"
            else:
                detail += f" (it was restored at its previous {loaded_ctx}-token context)"
            return _LoadOutcome(kind="load_failed", detail=detail, model=key)
        if not _wait_loaded(key, 30.0):
            return _LoadOutcome(
                kind="load_failed",
                detail=f"{key} reload returned but it never appeared as loaded",
                model=key,
            )
        confirmed = _confirmed_context(key) or (info or {}).get("context_length") or want
        return _LoadOutcome(
            model=key,
            context_length=confirmed,
            swapped=True,
            loaded_now=True,
            unloaded=[key],
            load_time_s=(info or {}).get("load_time_s"),
        )

    # Not loaded: an explicit load ADDS it alongside whatever is resident.
    residents = _resident_snapshot(models)
    # Ask-first room check: when the control page is reachable, classify the
    # fit up front. "too_large" is refused outright (an unload offer would be a
    # lie — no amount of freeing makes it fit); "needs_room" with the ask-first
    # policy returns the operator offer instead of a doomed load attempt.
    verdict, gpu_now = _room_verdict(entry.get("size_bytes"))
    size_mib = (entry.get("size_bytes") or 0) // (1024 * 1024)
    need_mib = size_mib + _ROOM_HEADROOM_MIB
    if verdict == "too_large":
        return _LoadOutcome(
            kind="model_too_large",
            detail=f"{key} needs ~{need_mib} MiB ({size_mib} MiB weights + "
            f"{_ROOM_HEADROOM_MIB} MiB headroom) but the GPU has only "
            f"{gpu_now.get('vram_total_mib')} MiB total — unloading a resident cannot "
            "make it fit; pick a smaller model or quantization",
            model=key,
            meta={"gpu": gpu_now},
        )
    if verdict == "needs_room" and residents and swap_policy() != "auto":
        offer = _unload_offer(
            "the free VRAM reported by the control page cannot fit this model with headroom",
            _resident_info(models, residents),
        )
        offer["gpu"] = gpu_now
        blocking = ", ".join(k for k, _i, _c in residents)
        return _LoadOutcome(
            kind="load_failed",
            detail=f"not enough free VRAM for {key}: ~{need_mib} MiB needed "
            f"(model + {_ROOM_HEADROOM_MIB} MiB headroom), "
            f"{gpu_now.get('vram_free_mib')} MiB free — blocking: {blocking}; "
            "unload one and retry, or set lmstudio_swap=auto",
            model=key,
            meta={"unload_offer": offer},
        )
    info, err, _status = _load_instance(key, want, load_timeout())
    unloaded: list[tuple[str, int]] = []
    swapped = False
    memory_proven = bool(err) and _looks_memory(err)
    crashed = bool(err) and _looks_engine_crash(err)
    crash_probe = ""
    context_reduced: dict | None = None
    if err and (memory_proven or crashed):
        # Cheaper steps first: a smaller KV cache may fit without unloading
        # anyone (context_default can far exceed what this prompt needs). This
        # step is non-destructive, so it is NOT gated on residents or the swap
        # policy — and for an unproven crash it doubles as the first probe.
        needed = min(want, _needed_context(est))
        attempted = want
        if needed < want:
            attempted = needed
            info, err, _status = _load_instance(key, needed, load_timeout())
            if err is None:
                context_reduced = {"from": want, "to": attempted}
        if err:
            # A crash with no memory wording is ambiguous: a failed KV
            # allocation and an unreadable file abort identically. A floor-
            # context load separates them (weights spill to CPU, so a readable
            # file loads at floor size). A crash is never taken as memory on
            # its word — only a probe that LOADS proves it.
            memory_proven = _looks_memory(err)
            crashed = _looks_engine_crash(err)
            if crashed and not memory_proven:
                if attempted > _FLOOR_CONTEXT:
                    proven, pdetail = _probe_crash_memory(key)
                    memory_proven = proven or _looks_memory(pdetail)
                    crash_probe = "loaded_at_floor" if proven else "failed_at_floor"
                    if not proven:
                        err = f"{err}; floor-context probe: {pdetail}"
                else:
                    crash_probe = "not_probed"
    if err and memory_proven and residents and swap_policy() == "auto":
        # It did not fit alongside them. A crashed load can leave the model
        # list stale, so re-read it before deciding who is actually resident.
        if crashed:
            fresh = _models_entries()
            if fresh is not None:
                residents = _resident_snapshot(fresh)
        # Then vacate residents ONE AT A TIME, retrying after each; never a
        # model with a chat in flight.
        for rkey, rid, rctx in residents:
            if err is None:
                break
            if not (_looks_memory(err) or _looks_engine_crash(err)):
                break
            with _unload_claim(rkey) as claimed:
                uerr = _unload_and_wait(rkey, rid) if claimed else None
            if not claimed:
                return _abandon_swap(
                    unloaded,
                    kind="busy",
                    detail=f"{rkey} is answering another ask_lms call, so it will not be "
                    f"unloaded to fit {key}; retry shortly",
                    model=key,
                )
            if uerr:
                return _abandon_swap(unloaded, kind="unload_failed", detail=uerr, model=key)
            unloaded.append((rkey, rctx))
            info, err, _status = _load_instance(key, want, load_timeout())
            swapped = True
    if err and swapped:
        detail = (
            f"could not load {key} after unloading {', '.join(k for k, _ in unloaded)}: "
            f"{err}"
        ) + _restore_clause(unloaded)
        return _LoadOutcome(
            kind="load_failed",
            detail=detail,
            model=key,
            unloaded=[k for k, _ in unloaded],
        )
    if err:
        extra = ""
        offer: dict = {}
        if crash_probe == "loaded_at_floor":
            extra += (
                f"; a {_FLOOR_CONTEXT}-token load of the same model succeeded, so the "
                "failure is memory-bound, not a bad file"
            )
        elif crash_probe == "failed_at_floor":
            extra += (
                "; the same model also failed at floor context, so the file itself looks "
                "broken — nothing was unloaded"
            )
        elif crash_probe == "not_probed":
            extra += (
                "; the prompt already needs the minimum window, so no smaller probe was "
                "possible — nothing was unloaded"
            )
        if residents and memory_proven and swap_policy() != "auto":
            who = ", ".join(k for k, _i, _c in residents)
            extra += f"; resident: {who} — unload one and retry, or set lmstudio_swap=auto"
            offer = {
                "unload_offer": _unload_offer(
                    "a different model is resident and this load did not fit",
                    _resident_info(models, residents),
                )
            }
        meta = dict(offer)
        if crash_probe:
            meta["crash_probe"] = crash_probe
        return _LoadOutcome(
            kind="load_failed",
            detail=f"could not load {key}: {err}{extra}",
            model=key,
            meta=meta,
        )
    if not _wait_loaded(key, 30.0):
        return _LoadOutcome(
            kind="load_failed",
            detail=f"{key} load returned but it never appeared as loaded",
            model=key,
        )
    after = _models_entries()
    displaced: list[str] = []
    displaced_unknown = after is None
    if after is not None:
        resident_after = {
            str(m["key"])
            for m in after
            if m.get("loaded") and m.get("type") in _CHAT_MODEL_TYPES
        }
        # Deliberately-unloaded models are reported under `unloaded`; `displaced`
        # is only for a resident that vanished WITHOUT us unloading it (an LM
        # Studio eviction, or a JIT instance's idle TTL expiring between reads).
        unloaded_keys = {k for k, _c in unloaded}
        displaced = [
            k for k, _i, _c in residents if k not in resident_after and k not in unloaded_keys
        ]
    confirmed = _confirmed_context(key) or (info or {}).get("context_length") or want
    outcome_meta: dict = {}
    if context_reduced:
        outcome_meta["context_reduced"] = context_reduced
    if crash_probe:
        outcome_meta["crash_probe"] = crash_probe
    return _LoadOutcome(
        model=key,
        context_length=confirmed,
        swapped=swapped,
        loaded_now=True,
        unloaded=[k for k, _c in unloaded],
        displaced=displaced,
        displaced_unknown=displaced_unknown,
        load_time_s=(info or {}).get("load_time_s"),
        meta=outcome_meta,
    )


def _ensure_loaded_blocking(model: str, est_prompt_tokens: int) -> _LoadOutcome:
    """Serialized load decision. Blocking by design — callers wrap it in a
    thread. The already-loaded fast path needs no lock; everything that might
    load takes the process lock and the cross-process file lock."""
    quick = _quick_loaded(model, est_prompt_tokens)
    if quick is not None:
        return quick
    with _LOAD_LOCK, _file_lock():
        return _ensure_loaded_locked(model, est_prompt_tokens)


def _split_thinking(text: str) -> tuple[str, str]:
    """Split a leading inline ``<think>…</think>`` block off the answer, for
    models whose reasoning LM Studio does not expose as ``reasoning_content``."""
    m = _THINK_RE.match(text or "")
    if not m:
        return (text or "").strip(), ""
    return ((text or "")[m.end() :] or "").strip(), (m.group(1) or "").strip()


def _parse_chat(obj: dict) -> tuple[str, str, str | None]:
    """``(text, thinking, error)`` from an OpenAI-compatible response object."""
    if isinstance(obj.get("error"), dict):
        e = obj["error"]
        return "", "", f"{e.get('type', 'error')}: {e.get('message', obj)}"
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "", "", "unrecognized response shape (no choices[])"
    msg = choices[0].get("message") or {}
    text = str(msg.get("content") or "").strip()
    thinking = str(msg.get("reasoning_content") or "").strip()
    if not thinking and text:
        text, thinking = _split_thinking(text)
    return text, thinking, None


def _truncated(ctx: int, est: int, reported_prompt: int, completion: int) -> bool:
    """Has LM Studio silently cut the prompt to fit?

    Primary signal is context-relative: a truncated prompt is pinned at the
    ceiling (prompt + reserved output ≈ the window). The estimate-relative
    fallback is used ONLY when the loaded window is unknown, because chars/token
    varies with the code's shape in both directions and a false "truncated" is
    its own cost (banner + no cache).
    """
    if reported_prompt <= 0:
        return False
    if ctx:
        return reported_prompt + completion >= ctx - 16
    return bool(est and reported_prompt < est * 0.5)


async def run(
    model: str,
    question: str,
    context: str = "",
    *,
    timeout: float | None = None,
) -> OracleResult:
    """Run one turn against a model on the configured LM Studio server. Never
    raises for expected failures (network, HTTP, timeout, missing model)."""
    model = (model or "").strip()
    if not configured():
        return OracleResult(
            "error",
            kind="not_configured",
            text="LM Studio not configured (set ASK_FABLE_LMSTUDIO_BASE_URL)",
            model=model,
        )
    if not model:
        return OracleResult(
            "error",
            kind="bad_args",
            text="no model given and no default configured (call list_lms_models and pass one)",
            model=model,
        )
    timeout = timeout if timeout is not None else chat_timeout()
    prompt = compose(question, context)
    # Include the system prompt: it is hundreds of tokens and ignoring it is how
    # a prompt passes the context check and still gets truncated.
    est = _estimate_tokens(ORACLE_SYSTEM_PROMPT) + _estimate_tokens(prompt)
    try:
        outcome = await asyncio.to_thread(_ensure_loaded_blocking, model, est)
    except Exception as exc:  # noqa: BLE001 — a bridge never raises at the oracle
        return OracleResult(
            "error", kind="sdk_error", text=f"LM Studio load path failed: {exc}", model=model
        )
    if not outcome.ok:
        return OracleResult(
            "error",
            kind=outcome.kind,
            text=outcome.detail,
            model=outcome.model or model,
            meta=outcome.meta,
        )

    key = outcome.model
    ctx = outcome.context_length
    cap = min(max_tokens(), max_output_tokens())
    if ctx:
        max_out = min(cap, ctx - est - _CONTEXT_MARGIN)
        if max_out < _MIN_OUTPUT_TOKENS:
            return OracleResult(
                "error",
                kind="context_too_small",
                text=f"{key} is loaded at {ctx} tokens; only ~{max_out} output tokens would fit "
                f"a ~{est}-token prompt — raise lmstudio_context / ASK_FABLE_LMSTUDIO_CONTEXT",
                model=key,
            )
    else:
        max_out = cap

    if not _inflight_add(key):
        return OracleResult(
            "error",
            kind="model_unavailable",
            text=f"{key} is being unloaded by another ask_lms call in this process (a swap "
            "or unload_lms_model); re-ask to reload it",
            model=key,
        )
    try:
        # F4: re-verify the resident window right before sending. Between the load decision
        # above and now the model may have been unloaded — a same-process unload that lost the
        # _inflight race, or ANY unload from a SECOND ask_fable process (which cannot see this
        # process's _INFLIGHT). Sending anyway JIT-reloads at LM Studio's app-default (~4k)
        # context and SILENTLY TRUNCATES the prompt — the worst failure class. Fail loudly and
        # retryably instead. (A failed catalog read is left to _chat, which surfaces it.)
        live = await asyncio.to_thread(_models_entries)
        if live is not None:
            entry = _find_model(live, key)
            if entry is None or not entry.get("loaded"):
                return OracleResult(
                    "error",
                    kind="model_unavailable",
                    text=f"{key} was unloaded between the load check and the send (likely a "
                    "concurrent unload from another ask_fable process); re-ask to reload it",
                    model=key,
                )
            live_ctx = int(entry.get("loaded_context_length") or 0)
            if live_ctx and live_ctx < _needed_context(est):
                return OracleResult(
                    "error",
                    kind="context_too_small",
                    text=f"{key} is now resident at only {live_ctx} tokens but this prompt "
                    f"needs ~{_needed_context(est)}; it was reloaded smaller — re-ask, or pin "
                    "lmstudio_context / ASK_FABLE_LMSTUDIO_CONTEXT",
                    model=key,
                )
        return await _chat(
            key,
            model,
            prompt,
            est=est,
            ctx=ctx,
            max_out=max_out,
            timeout=timeout,
            outcome=outcome,
        )
    finally:
        _inflight_done(key)


async def _chat(
    key: str,
    requested: str,
    prompt: str,
    *,
    est: int,
    ctx: int,
    max_out: int,
    timeout: float,
    outcome: _LoadOutcome,
) -> OracleResult:
    messages = [
        {"role": "system", "content": ORACLE_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    payload = {"model": key, "messages": messages, "stream": False, "max_tokens": max_out}

    final_http_status: int | None = None
    retry_count = 0

    def _call() -> tuple[dict | None, str | None]:
        nonlocal final_http_status
        with _generating(key):  # holds the model until THIS thread is done
            obj, err, status = _request(
                "/v1/chat/completions", method="POST", payload=payload, timeout=timeout
            )
        final_http_status = status
        return obj, err

    started = time.perf_counter()
    try:
        (obj, err), status_retries = await call_with_transient_retry(
            _call,
            timeout=timeout,
            retryable=lambda r: bool(r[1]) and final_http_status in RETRYABLE_HTTP_STATUSES,
        )
        retry_count += status_retries
    except TransientRetryTimeout as e:
        return OracleResult(
            "error",
            kind="timeout",
            text=f"{key} timed out after {e.elapsed_s:.0f}s",
            model=key,
            telemetry=ProviderTelemetry(
                oracle_key="lmstudio",
                requested_model=requested,
                actual_model=key,
                transport="http-json",
                retry_count=retry_count + e.retry_count,
                wall_duration_ms=(time.perf_counter() - started) * 1000,
                reasoning_available=False,
                usage_available=False,
                tools_available=False,
            ),
        )
    if err or obj is None:
        kind, text = http_error_detail(
            label="LM Studio", error=err or "empty response", http_status=final_http_status
        )
        return OracleResult(
            "error",
            kind=kind,
            text=text,
            model=key,
            telemetry=ProviderTelemetry(
                oracle_key="lmstudio",
                requested_model=requested,
                actual_model=key,
                transport="http-json",
                http_status=final_http_status,
                retry_count=retry_count,
                wall_duration_ms=(time.perf_counter() - started) * 1000,
                reasoning_available=False,
                usage_available=False,
                tools_available=False,
            ),
        )

    text, thinking, parse_err = _parse_chat(obj)
    if parse_err:
        kind, detail = http_error_detail(label="LM Studio", error=parse_err)
        return OracleResult("error", kind=kind, text=detail, model=key)
    finish = finish_reason(obj)
    capped = str(finish or "").strip().lower() in CAPPED_STOP_REASONS
    if not text:
        spent = bool(thinking) or capped
        detail = (
            f"the model spent all {max_out} output tokens reasoning and returned no answer "
            f"({len(thinking)} chars of thinking) — raise lmstudio_max_tokens "
            "(ASK_FABLE_LMSTUDIO_MAX_TOKENS) or ask narrower"
            if spent
            else "empty response from model"
        )
        # A spent output budget is the request's size, not the server's health: its own
        # non-health kind, so it never pushes the circuit breaker open.
        kind = "budget_exhausted" if spent else "sdk_error"
        return OracleResult("error", kind=kind, text=detail, model=key)
    if text.startswith("REFUSED:"):
        reason = text[len("REFUSED:") :].strip() or "off-scope"
        return OracleResult("refused", text=reason, model=key, thinking=thinking)

    raw_usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    usage = (
        usage_if_available(
            ProviderUsage(
                input_tokens=token_count(raw_usage.get("prompt_tokens")),
                output_tokens=token_count(raw_usage.get("completion_tokens")),
                total_tokens=token_count(raw_usage.get("total_tokens")),
            )
        )
        if raw_usage
        else None
    )

    kind = ""
    reported_prompt = token_count(raw_usage.get("prompt_tokens")) or 0
    completion = token_count(raw_usage.get("completion_tokens")) or 0
    if _truncated(ctx, est, reported_prompt, completion):
        # Say so, and never let a truncated answer be cached.
        kind = "truncated"
        text = (
            f"[ask_fable warning: LM Studio reported {reported_prompt} prompt tokens "
            f"(estimated ~{est}) against a {ctx or '?'}-token window; the prompt was "
            "likely truncated — raise lmstudio_context / ASK_FABLE_LMSTUDIO_CONTEXT for "
            f"a complete answer]\n\n{text}"
        )

    meta: dict = {"model_loaded": True}
    if ctx:
        meta["loaded_context_length"] = ctx
    else:
        meta["context_unverified"] = True
    if outcome.loaded_now:
        meta["load_confirmed"] = True
    if outcome.swapped:
        meta["swapped"] = True
        meta["unloaded"] = outcome.unloaded
    if outcome.swapped or outcome.displaced:
        # The confirmation the operator asked for: even an empty list says
        # "nothing else vanished".
        meta["displaced"] = outcome.displaced
    if outcome.displaced_unknown:
        meta["displaced_unknown"] = True
    if outcome.load_time_s is not None:
        meta["load_seconds"] = outcome.load_time_s
    if finish:
        meta["finish_reason"] = finish
    if capped:
        # The output cap cut the answer off: still returned, but flagged so no cache
        # layer pins a partial answer as complete (see oracle_common.shape_stopped).
        kind = "truncated"
        meta["partial"] = True
    if outcome.meta:
        # Load-path evidence (e.g. context_reduced, crash_probe) travels with a
        # successful answer too — never only in failures.
        meta.update(outcome.meta)

    telemetry = ProviderTelemetry(
        oracle_key="lmstudio",
        requested_model=requested,
        actual_model=str(obj.get("model") or key),
        transport="http-json",
        stop_reason=finish,
        http_status=final_http_status,
        retry_count=retry_count,
        wall_duration_ms=(time.perf_counter() - started) * 1000,
        reasoning_available=bool(thinking),
        usage_available=usage is not None,
        tools_available=False,
        usage=usage,
    )
    return OracleResult(
        "ok",
        text=text,
        kind=kind,
        model=str(obj.get("model") or key),
        thinking=thinking,
        telemetry=telemetry,
        meta=meta,
    )
