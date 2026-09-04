"""Reach an Atlas Cloud text model for one reasoning turn.

Dynamic council oracles named ``atlas:<model>`` (e.g. ``atlas:xai/grok-4.6``,
``atlas:openai/gpt-5.6-sol``) go through here. Atlas Cloud speaks the standard
OpenAI ``/v1/chat/completions`` shape: a Bearer auth header, a system message,
and a response with ``choices[0].message.content`` (+ a standard ``usage``
block). So this is its own backend — not a reuse of ``anthropic_http``, which
speaks the Anthropic Messages shape, and not the Ollama native ``/api/chat``
shape.

Auth: ``ASK_FABLE_ATLAS_API_KEY`` if set, falling back to the
``ATLASCLOUD_API_KEY`` the Atlas Cloud MCP server already uses (so the operator
doesn't set it twice). The catalog endpoint (``GET /api/v1/models``) needs NO
auth; only the chat endpoint needs the key. A plain stdlib ``urllib`` POST (no
new dependency); the same ``REFUSED:`` scope contract as every other oracle.

Effort: the selection menu offers three presets — ``quick`` / ``standard`` /
``deep`` — that map to ``max_tokens`` + timeout + a system-prompt nudge, since
Atlas's chat API documents no ``reasoning_effort``. ``deep`` opportunistically
sends ``reasoning_effort: "high"`` and retries without it on a 400, mirroring
ollama's ``think``-flag retry.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import config
from .openai_compat import (  # shared OpenAI-compatible plumbing (see openai_compat.py)
    DEFAULT_EFFORT,
    EFFORT_PRESETS,
    effort_menu,
)
from .openai_compat import (
    USER_AGENT as _USER_AGENT,
)
from .openai_compat import finish_reason as _finish_reason
from .openai_compat import get_json as _get_json
from .openai_compat import max_tokens_cap as _max_tokens
from .openai_compat import parse_chat_response as _parse
from .oracle_common import (
    RETRYABLE_HTTP_STATUSES,
    OracleResult,
    TransientRetryTimeout,
    call_with_transient_retry,
    compose,
    http_error_detail,
    shape,
)
from .prompts import ORACLE_SYSTEM_PROMPT  # same scope contract for every oracle
from .provider_telemetry import ProviderTelemetry, ProviderUsage, token_count, usage_if_available

DEFAULT_ATLAS_BASE_URL = "https://api.atlascloud.ai"
DEFAULT_ATLAS_MODEL = "xai/grok-4.6"  # strong reasoner, cheap ($2/$6 per M), 500k ctx, NEW+HOT
DEFAULT_ATLAS_SYNTH_MODEL = "openai/gpt-5.6-sol"  # ask_atlas_council's adjudicator when the local codex CLI is absent
CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/api/v1/models"  # no auth required — the whole catalog in one GET

# Tag rank for the ``featured`` shortlist — HOT first, then NEW.
_TAG_RANK = {"HOT": 0, "NEW": 1}

_TASK_SIGNALS = {
    "coding": frozenset(
        {
            "api",
            "backend",
            "bug",
            "cli",
            "code",
            "coding",
            "debug",
            "frontend",
            "go",
            "golang",
            "javascript",
            "program",
            "programming",
            "python",
            "refactor",
            "repo",
            "repository",
            "rust",
            "service",
            "software",
            "typescript",
        }
    ),
    "reasoning": frozenset(
        {
            "analysis",
            "analyze",
            "architecture",
            "complex",
            "decision",
            "design",
            "math",
            "plan",
            "planning",
            "proof",
            "reason",
            "reasoning",
            "research",
        }
    ),
    "agentic": frozenset(
        {
            "agent",
            "agentic",
            "autonomous",
            "multi-step",
            "tool",
            "tools",
            "workflow",
        }
    ),
    "creative": frozenset(
        {
            "brainstorm",
            "content",
            "creative",
            "marketing",
            "story",
            "write",
            "writing",
        }
    ),
    "chat": frozenset(
        {
            "assistant",
            "chat",
            "conversation",
            "conversational",
            "customer",
            "support",
        }
    ),
    "multimodal": frozenset(
        {
            "audio",
            "image",
            "multimodal",
            "ocr",
            "screenshot",
            "video",
            "vision",
        }
    ),
    "fast": frozenset(
        {
            "fast",
            "instant",
            "interactive",
            "latency",
            "quick",
            "rapid",
            "realtime",
            "responsive",
            "speed",
        }
    ),
    "cheap": frozenset(
        {
            "affordable",
            "budget",
            "cheap",
            "cost",
            "economical",
            "inexpensive",
        }
    ),
    "long_context": frozenset(
        {
            "codebase",
            "context",
            "document",
            "documents",
            "large",
            "long",
            "many",
            "repo",
            "repository",
        }
    ),
}

_MODEL_SIGNALS = {
    "coding": frozenset(
        {
            "code",
            "coder",
            "coding",
            "debugging",
            "developer",
            "programming",
            "refactoring",
            "repository",
            "software",
        }
    ),
    "reasoning": frozenset(
        {
            "analysis",
            "analytical",
            "complex",
            "problem-solving",
            "reasoning",
            "research",
        }
    ),
    "agentic": frozenset(
        {
            "agent",
            "agentic",
            "autonomous",
            "multi-step",
            "tool",
            "tool-calling",
            "workflow",
        }
    ),
    "creative": frozenset(
        {
            "content",
            "conversation",
            "creative",
            "story",
            "writing",
        }
    ),
    "chat": frozenset(
        {
            "assistant",
            "chat",
            "conversation",
            "conversational",
            "dialogue",
            "support",
        }
    ),
    "multimodal": frozenset(
        {
            "audio",
            "image",
            "multimodal",
            "ocr",
            "video",
            "vision",
        }
    ),
}

_WORD_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)?")


def base_url() -> str:
    raw = (os.environ.get("ASK_FABLE_ATLAS_BASE_URL") or "").strip() or DEFAULT_ATLAS_BASE_URL
    return raw.rstrip("/")


def api_key() -> str | None:
    """The Atlas Cloud API key, or None.

    Precedence: ``ASK_FABLE_ATLAS_API_KEY`` (the ask_fable convention) →
    ``ATLASCLOUD_API_KEY`` (the key the Atlas Cloud MCP server already uses, so
    the operator doesn't have to set it twice)."""
    for name in ("ASK_FABLE_ATLAS_API_KEY", "ATLASCLOUD_API_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return None


def configured() -> bool:
    """True when the chat endpoint can be reached — i.e. an API key is present.
    The catalog endpoint needs no auth, but a real reasoning call does."""
    return api_key() is not None


def default_model() -> str:
    """Model used by the ``ask_atlas`` tool when none is passed.

    Precedence: config file (``atlas_model``) → ``ASK_FABLE_ATLAS_MODEL`` →
    ``DEFAULT_ATLAS_MODEL``."""
    return (
        config.get_str("atlas_model")
        or (os.environ.get("ASK_FABLE_ATLAS_MODEL") or "").strip()
        or DEFAULT_ATLAS_MODEL
    )


def dedupe_models(parts: list[str]) -> list[str]:
    """Strip an optional ``atlas:`` prefix off each id and de-dupe, order-preserving.
    Ids keep their casing (Atlas ids are case-SENSITIVE — lowercasing e.g.
    ``deepseek-ai/DeepSeek-V3.1-Terminus`` turns it into an HTTP 400); the
    de-dupe itself is case-insensitive, keeping the first-seen spelling."""
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        m = p[len("atlas:"):] if p.lower().startswith("atlas:") else p
        m = m.strip()
        if m and m.lower() not in seen:
            seen.add(m.lower())
            out.append(m)
    return out


def council_models() -> list[str]:
    """Atlas Cloud model ids for a default ``ask_atlas_council``.

    Precedence, highest first: the config file (``atlas_council``, written by the
    ``configure_atlas_council`` tool) → ``ASK_FABLE_ATLAS_COUNCIL`` (comma- or
    whitespace-separated) → ``[]`` (the handler then derives a small panel from
    the live featured catalog). Returns bare model ids — the ``atlas:`` prefix is
    added by callers."""
    from_config = config.get_list("atlas_council")
    if from_config:
        return dedupe_models(from_config)
    raw = os.environ.get("ASK_FABLE_ATLAS_COUNCIL") or ""
    return dedupe_models([p for p in raw.replace(",", " ").split() if p.strip()])


def synthesizer_token() -> str | None:
    """Persisted synthesizer override for ``ask_atlas_council``, or None.

    Precedence: config file (``atlas_synthesizer``) → ``ASK_FABLE_ATLAS_SYNTHESIZER``.
    Any council token works ('codex', 'fable', 'atlas:<model-id>', or a bare Atlas
    id like 'openai/gpt-5.6-sol'); None keeps the built-in GPT-first ladder."""
    return (
        config.get_str("atlas_synthesizer")
        or (os.environ.get("ASK_FABLE_ATLAS_SYNTHESIZER") or "").strip()
        or None
    )


def default_effort() -> str:
    """Effective default effort for Atlas (and Atlas-routed Grok tokens).

    Precedence: config ``atlas_effort`` / ``effort`` → ``ASK_FABLE_ATLAS_EFFORT``
    / ``ASK_FABLE_EFFORT`` → ``DEFAULT_EFFORT`` (``deep``). Invalid values fall
    back to ``deep`` so "high if possible" stays the default posture.
    """
    raw = (
        config.get_str("atlas_effort")
        or config.get_str("effort")
        or (os.environ.get("ASK_FABLE_ATLAS_EFFORT") or "").strip()
        or (os.environ.get("ASK_FABLE_EFFORT") or "").strip()
        or DEFAULT_EFFORT
    )
    name = raw.strip().lower()
    return name if name in EFFORT_PRESETS else DEFAULT_EFFORT


def effort_preset(effort: str | None) -> dict:
    """Resolve an effort name to its preset, falling back to :func:`default_effort`."""
    name = (effort or "").strip().lower() or default_effort()
    return EFFORT_PRESETS.get(name, EFFORT_PRESETS[default_effort()])


def _model_item(m: dict) -> dict | None:
    """Turn one raw catalog entry into a menu item, or None if it should be hidden."""
    mid = str(m.get("model") or "").strip()
    if not mid:
        return None
    price = m.get("price") or {}
    actual = price.get("actual") if isinstance(price, dict) else None
    actual = actual if isinstance(actual, dict) else {}
    inp = actual.get("input_price")
    out = actual.get("output_price")
    cache = actual.get("cache_price")
    display_name = str(m.get("displayName") or mid)
    tags = m.get("tags") if isinstance(m.get("tags"), list) else []
    ctx = m.get("contextLength")
    lat = m.get("avgLatency")
    profile = str(m.get("profile") or "").strip()
    return {
        "label": display_name,
        "model_id": mid,
        "display_name": display_name,
        "description": _describe(inp, out, ctx, lat, profile),
        "cost_note": f"${inp}/${out} per M" if (inp and out) else None,
        "provider": str(m.get("organization") or ""),
        "tags": [str(t) for t in tags],
        "context_length": ctx if isinstance(ctx, int) else None,
        "max_output": m.get("maxCompletionTokens")
        if isinstance(m.get("maxCompletionTokens"), int)
        else None,
        "avg_latency_s": _number(lat),
        "input_price": inp,
        "output_price": out,
        "cache_price": cache,
        "profile": profile,
    }


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _describe(inp, out, ctx, lat, profile) -> str:
    parts: list[str] = []
    if inp and out:
        parts.append(f"${inp}/${out} per M")
    if isinstance(ctx, int):
        parts.append(f"{ctx // 1000}k ctx")
    if isinstance(lat, (int, float)):
        parts.append(f"~{lat}s latency")
    prof = str(profile or "").strip()
    if prof:
        parts.append(prof[:140])  # one-line description tail
    return " · ".join(parts)


def _featured(items: list[dict], limit: int = 8) -> list[dict]:
    """A curated shortlist derived from tags + provider dedup (never hardcoded,
    so it never goes stale as Atlas updates the catalog). HOT/NEW-tagged models
    first, at most one per provider until the limit is reached; remaining slots
    filled by tag rank then name."""

    def tag_rank(it: dict) -> int:
        return min((_TAG_RANK[t] for t in it["tags"] if t in _TAG_RANK), default=99)

    ranked = sorted(items, key=lambda it: (tag_rank(it), it["provider"], it["label"].lower()))
    out: list[dict] = []
    chosen: set[str] = set()
    seen_providers: set[str] = set()
    # Pass 1: HOT/NEW, one per provider.
    for it in ranked:
        if tag_rank(it) < 99 and it["provider"] not in seen_providers:
            out.append(it)
            chosen.add(it["model_id"])
            seen_providers.add(it["provider"])
        if len(out) >= limit:
            break
    # Pass 2: fill remaining slots by rank, regardless of provider.
    for it in ranked:
        if len(out) >= limit:
            break
        if it["model_id"] not in chosen:
            out.append(it)
            chosen.add(it["model_id"])
    return out


def recommend_models(items: list[dict], task: str, limit: int = 5) -> list[dict]:
    task_tokens = set(_WORD_RE.findall(task.lower()))
    requested = {
        capability for capability, signals in _TASK_SIGNALS.items() if task_tokens & signals
    }
    max_context = max((item.get("context_length") or 0 for item in items), default=0)

    ranked: list[dict] = []
    for item in items:
        model_tokens = set(
            _WORD_RE.findall(
                " ".join(
                    (
                        str(item.get("label") or ""),
                        str(item.get("profile") or ""),
                        " ".join(item.get("tags") or []),
                    )
                ).lower()
            )
        )
        score = float(len(task_tokens & model_tokens) * 2)
        reasons: list[str] = []

        for capability in sorted(requested & set(_MODEL_SIGNALS)):
            if model_tokens & _MODEL_SIGNALS[capability]:
                score += 8
                reasons.append(capability.replace("_", " "))

        if "CODE" in item.get("tags", []) and "coding" in requested:
            score += 5
            if "coding" not in reasons:
                reasons.append("coding")
        if "long_context" in requested and max_context:
            context_length = item.get("context_length") or 0
            score += 6 * context_length / max_context
            if context_length:
                reasons.append(f"{context_length // 1000}k context")
        if "fast" in requested:
            latency = item.get("avg_latency_s")
            if latency is not None:
                score += 5 / (1 + latency)
                reasons.append(f"~{latency:g}s latency")
        if "cheap" in requested:
            input_price = _number(item.get("input_price"))
            output_price = _number(item.get("output_price"))
            if input_price is not None and output_price is not None:
                score += 8 / (1 + input_price + output_price)
                reasons.append(item.get("cost_note") or "low cost")
        if "HOT" in item.get("tags", []):
            score += 1
        if "NEW" in item.get("tags", []):
            score += 0.5
        if "CODE" in item.get("tags", []) and "coding" not in requested:
            score -= 3
        if "ocr" in model_tokens and "multimodal" not in requested:
            score -= 10
        if not reasons:
            reasons.append("current catalog fit")

        picker_parts = reasons[:2]
        if item.get("cost_note") and item.get("cost_note") not in picker_parts:
            picker_parts.append(item["cost_note"])
        ranked.append(
            {
                **item,
                "match_score": round(score, 3),
                "match_reasons": reasons,
                "picker_description": " · ".join(picker_parts),
            }
        )

    ranked.sort(key=lambda item: (-item["match_score"], item["label"].lower()))
    result_limit = max(1, min(limit, 8))
    selected: list[dict] = []
    selected_models: set[str] = set()
    selected_providers: set[str] = set()
    for item in ranked:
        provider = item.get("provider") or item["model_id"].split("/", 1)[0]
        if provider not in selected_providers:
            selected.append(item)
            selected_models.add(item["model_id"])
            selected_providers.add(provider)
        if len(selected) >= result_limit:
            return selected
    for item in ranked:
        if item["model_id"] not in selected_models:
            selected.append(item)
        if len(selected) >= result_limit:
            break
    return selected


def catalog(
    timeout: float = 6.0,
    *,
    task: str = "",
    recommendation_limit: int = 5,
) -> dict:
    """Discover Atlas text models and build a menu (best-effort, never raises).

    Returns ``{"cloud_ok", "featured", "menu", "effort_choices", "models", "balance_note"}``:
      - ``featured``: ~8 curated models (HOT/NEW-tagged, one per provider) — the
        shortlist an agent should show first.
      - ``menu``: every visible text model, each ``{label, model_id, description,
        cost_note, provider, tags, ...}``.
      - ``effort_choices``: the three effort presets (so the agent renders both
        the model picker and the effort picker from one call).
      - ``models``: bare model ids in menu order.
      - ``balance_note``: None (the balance endpoint isn't part of the public
        no-auth contract; per-model ``cost_note`` is the actionable cost signal).
    """
    raw = _get_json(f"{base_url()}{MODELS_PATH}", {"accept": "application/json"}, timeout)
    if not raw or not isinstance(raw, dict):
        return {
            "cloud_ok": False,
            "featured": [],
            "menu": [],
            "effort_choices": effort_menu(),
            "models": [],
            "balance_note": None,
            "hint": "could not reach the Atlas models endpoint",
        }
    items: list[dict] = []
    for m in raw.get("data") or []:
        if not isinstance(m, dict):
            continue
        if (m.get("type") or "").lower() != "text":
            continue
        if not m.get("display_console", True):
            continue  # internal model — the skill warns these won't work for callers
        item = _model_item(m)
        if item:
            items.append(item)
    items.sort(key=lambda it: it["label"].lower())
    recommendations = recommend_models(items, task, recommendation_limit) if task.strip() else []
    result = {
        "cloud_ok": True,
        "featured": _featured(items),
        "menu": items,
        "effort_choices": effort_menu(),
        "models": [it["model_id"] for it in items],
        "balance_note": None,
    }
    if recommendations:
        result.update(
            {
                "task": task.strip(),
                "recommendations": recommendations,
                "recommendation_basis": "live_catalog_metadata",
                "recommendation_note": (
                    "Ranked from Atlas capability profiles, tags, context, latency, and price; "
                    "this is not an independent benchmark."
                ),
                "picker": {
                    "title": "Choose an Atlas model",
                    "question": f"Which model should handle: {task.strip()}",
                    "model_options": [
                        {
                            "value": item["model_id"],
                            "label": item["label"],
                            "description": item["picker_description"],
                        }
                        for item in recommendations
                    ],
                    "effort_options": effort_menu(),
                },
            }
        )
    return result


async def run(
    model: str,
    question: str,
    context: str = "",
    *,
    effort: str | None = None,
    timeout: float | None = None,
) -> OracleResult:
    """Run one turn against an Atlas Cloud text model. Never raises for expected
    failures (network, HTTP, timeout, bad key, missing config)."""
    preset = effort_preset(effort)
    timeout = timeout if timeout is not None else preset["timeout"]
    key = api_key()
    if not configured():
        return OracleResult(
            "error",
            kind="not_configured",
            text="atlas not configured (set ASK_FABLE_ATLAS_API_KEY, or ATLASCLOUD_API_KEY "
            "which the Atlas Cloud MCP server already uses)",
            model=model,
        )

    sys_prompt = ORACLE_SYSTEM_PROMPT
    nudge = preset.get("nudge")
    if nudge:
        sys_prompt = f"{ORACLE_SYSTEM_PROMPT}\n\n{nudge}"
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": compose(question, context)},
    ]

    cap = min(preset["max_tokens"], _max_tokens())
    metadata: dict = {}
    retry_count = 0
    final_http_status: int | None = None

    def _post(with_reasoning_effort: bool) -> tuple[str | None, dict, str | None, int | None]:
        request_body: dict = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": cap,
            "stream": False,
        }
        if with_reasoning_effort and preset.get("reasoning_effort"):
            request_body["reasoning_effort"] = preset["reasoning_effort"]
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {key}",
            "user-agent": _USER_AGENT,  # same WAF guard as the catalog GET
        }
        req = urllib.request.Request(
            f"{base_url()}{CHAT_PATH}",
            data=json.dumps(request_body).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                try:
                    metadata.clear()  # a retry must not inherit a prior body
                    metadata.update(json.loads(body))
                except (ValueError, UnicodeDecodeError):
                    pass
                text, parsed, err = _parse(body)
                return text, parsed, err, 200
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            return None, {}, f"HTTP {e.code}: {detail or e.reason}", e.code
        except urllib.error.URLError as e:
            return None, {}, f"network error: {e.reason}", None

    def _call() -> tuple[str | None, str | None, str | None]:
        nonlocal final_http_status, retry_count
        use_re = bool(preset.get("reasoning_effort"))
        text, _parsed, err, code = _post(use_re)
        final_http_status = code
        # Some gateways reject the undocumented `reasoning_effort` field with a
        # 400 — retry once without it (only relevant on the `deep` preset). Only
        # when the error actually reads like a rejected field, though: a
        # permanent 400 (oversized context, invalid request) re-POSTed without
        # reasoning_effort fails identically and just burns quota twice.
        lowered = (err or "").lower()
        field_rejected = any(
            s in lowered
            for s in ("reasoning", "effort", "unsupported", "unknown", "unrecognized", "unexpected")
        )
        if code == 400 and use_re and field_rejected:
            retry_count += 1
            text, _parsed, err, final_http_status = _post(False)
        return text, None, err

    started = time.perf_counter()
    try:
        (text, _thinking, err), status_retries = await call_with_transient_retry(
            _call,
            timeout=timeout,
            # One bounded retry on transient statuses (429 / 5xx) — the
            # reasoning_effort feature retry inside _call can stack on top.
            retryable=lambda r: bool(r[2]) and final_http_status in RETRYABLE_HTTP_STATUSES,
        )
        retry_count += status_retries
    except TransientRetryTimeout as e:
        return OracleResult(
            "error",
            kind="timeout",
            text=f"{model} timed out after {e.elapsed_s:.0f}s",
            model=model,
            telemetry=ProviderTelemetry(
                oracle_key="atlas",
                requested_model=model,
                actual_model=model,
                transport="http-json",
                retry_count=retry_count + e.retry_count,
                wall_duration_ms=(time.perf_counter() - started) * 1000,
                reasoning_available=False,
                usage_available=False,
                tools_available=False,
            ),
        )
    if err:
        kind, text = http_error_detail(label="Atlas", error=err, http_status=final_http_status)
        return OracleResult(
            "error",
            kind=kind,
            text=text,
            model=model,
            telemetry=ProviderTelemetry(
                oracle_key="atlas",
                requested_model=model,
                actual_model=model,
                transport="http-json",
                http_status=final_http_status,
                retry_count=retry_count,
                wall_duration_ms=(time.perf_counter() - started) * 1000,
                reasoning_available=False,
                usage_available=False,
                tools_available=False,
            ),
        )
    shaped = shape(text)
    usage_obj = metadata.get("usage") if isinstance(metadata, dict) else None
    usage = (
        usage_if_available(
            ProviderUsage(
                input_tokens=token_count(usage_obj.get("prompt_tokens"))
                if isinstance(usage_obj, dict)
                else None,
                output_tokens=token_count(usage_obj.get("completion_tokens"))
                if isinstance(usage_obj, dict)
                else None,
                total_tokens=token_count(usage_obj.get("total_tokens"))
                if isinstance(usage_obj, dict)
                else None,
            )
        )
        if isinstance(usage_obj, dict)
        else None
    )
    telemetry = ProviderTelemetry(
        oracle_key="atlas",
        requested_model=model,
        actual_model=metadata.get("model") or model,
        transport="http-json",
        stop_reason=_finish_reason(metadata),
        http_status=final_http_status,
        retry_count=retry_count,
        wall_duration_ms=(time.perf_counter() - started) * 1000,
        reasoning_available=False,
        usage_available=usage is not None,
        tools_available=False,
        usage=usage,
    )
    return OracleResult(
        shaped.status,
        text=shaped.text,
        kind=shaped.kind,
        model=model,
        thinking="",
        telemetry=telemetry,
    )
