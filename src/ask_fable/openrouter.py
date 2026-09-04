"""OpenRouter as an oracle backend — one gateway, ~400 models, one API key.

The sibling of ``atlas.py``: both are OpenAI-compatible gateways, and everything
they share lives in ``openai_compat.py``. What is here is what OpenRouter does
differently, and the differences are real enough that a shared ``run()`` would be
a base class made of feature flags:

* **The catalog is self-describing.** Every model publishes
  ``supported_parameters``, a ``reasoning`` block (``supported_efforts``,
  ``default_effort``), ``context_length`` and per-token ``pricing``. Atlas has to
  guess whether a model accepts ``reasoning_effort`` and retry without it on a
  400; here we simply read what the model accepts and send only that. So there is
  no blind feature-probe retry in this module — it would be a worse version of
  information we already have.
* **The ranking is data, not keywords.** Atlas ranks a flat catalog with a
  hand-tuned keyword table; OpenRouter gives us price, context, reasoning support
  and release date, so ``recommend_models`` scores those directly. Nothing here
  needs a curated list of model families to stay current.
* **Responses say who actually served them.** ``provider`` names the upstream and
  ``usage.cost`` is the real dollar cost of the call, which lands straight in
  ``ProviderUsage.cost_usd`` — most backends can only estimate it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from . import config
from .openai_compat import (
    DEFAULT_EFFORT,
    EFFORT_PRESETS,
    USER_AGENT,
    effort_menu,
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
from .provider_telemetry import (
    ProviderTelemetry,
    ProviderUsage,
    normalize_cost_usd,
    token_count,
    usage_if_available,
)

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Strong reasoner at a fraction of frontier pricing, 1M context — the same
# "capable but cheap" posture as the Atlas default.
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4-pro"
DEFAULT_OPENROUTER_SYNTH_MODEL = "openai/gpt-5.6-sol"
CHAT_PATH = "/chat/completions"
MODELS_PATH = "/models"  # no auth required — the whole catalog in one GET

# OpenRouter attributes traffic to an app via these; they are optional, and they
# only affect how the call is labelled on the dashboard.
_REFERER = "https://github.com/baggybin/ask-fable"
_TITLE = "ask-fable"

# Batch endpoints are async and priced differently; an interactive oracle call
# must never be routed to one. Alias rows (`~vendor/model-latest`) are pointers
# at another row, so they would double up the menu.
_BATCH_SUFFIX = ":batch"
_ALIAS_PREFIX = "~"


def base_url() -> str:
    raw = (
        os.environ.get("ASK_FABLE_OPENROUTER_BASE_URL") or ""
    ).strip() or DEFAULT_OPENROUTER_BASE_URL
    return raw.rstrip("/")


def api_key() -> str | None:
    """The OpenRouter API key, or None.

    Precedence: ``ASK_FABLE_OPENROUTER_API_KEY`` (the ask_fable convention) →
    ``OPENROUTER_API_KEY`` (what most OpenRouter tooling already sets, so the
    operator doesn't have to configure it twice)."""
    for name in ("ASK_FABLE_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return None


def configured() -> bool:
    """True when the chat endpoint can be reached — i.e. an API key is present.
    The catalog endpoint needs no auth, but a real reasoning call does."""
    return api_key() is not None


def default_model() -> str:
    """Model used by the ``ask_openrouter`` tool when none is passed.

    Precedence: config file (``openrouter_model``) → ``ASK_FABLE_OPENROUTER_MODEL``
    → ``DEFAULT_OPENROUTER_MODEL``."""
    return (
        config.get_str("openrouter_model")
        or (os.environ.get("ASK_FABLE_OPENROUTER_MODEL") or "").strip()
        or DEFAULT_OPENROUTER_MODEL
    )


def dedupe_models(parts: list[str]) -> list[str]:
    """De-dupe model ids, preserving first-seen order and casing."""
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        m = str(p).strip()
        if m and m.lower() not in seen:
            seen.add(m.lower())
            out.append(m)
    return out


def council_models() -> list[str]:
    """OpenRouter model ids for a default ``ask_openrouter_council``.

    Precedence: config file (``openrouter_council``, written by
    ``configure_openrouter_council``) → ``ASK_FABLE_OPENROUTER_COUNCIL`` (comma-
    or whitespace-separated) → ``[]`` (the handler then derives a panel from the
    live featured catalog). Returns bare model ids — the ``openrouter:`` prefix
    is added by callers."""
    from_config = config.get_list("openrouter_council")
    if from_config:
        return dedupe_models(from_config)
    raw = os.environ.get("ASK_FABLE_OPENROUTER_COUNCIL") or ""
    return dedupe_models([p for p in raw.replace(",", " ").split() if p.strip()])


def synthesizer_token() -> str | None:
    """Persisted synthesizer override for ``ask_openrouter_council``, or None."""
    return (
        config.get_str("openrouter_synthesizer")
        or (os.environ.get("ASK_FABLE_OPENROUTER_SYNTHESIZER") or "").strip()
        or None
    )


def default_effort() -> str:
    """Effective default effort. Precedence: config ``openrouter_effort`` /
    ``effort`` → ``ASK_FABLE_OPENROUTER_EFFORT`` / ``ASK_FABLE_EFFORT`` →
    ``deep``."""
    raw = (
        config.get_str("openrouter_effort")
        or config.get_str("effort")
        or (os.environ.get("ASK_FABLE_OPENROUTER_EFFORT") or "").strip()
        or (os.environ.get("ASK_FABLE_EFFORT") or "").strip()
        or DEFAULT_EFFORT
    )
    name = raw.strip().lower()
    return name if name in EFFORT_PRESETS else DEFAULT_EFFORT


def effort_preset(effort: str | None) -> dict:
    """Resolve an effort name to its preset, falling back to :func:`default_effort`."""
    name = (effort or "").strip().lower() or default_effort()
    return EFFORT_PRESETS.get(name, EFFORT_PRESETS[default_effort()])


# --- catalog ---------------------------------------------------------------


def _price_per_m(pricing: dict, key: str) -> float | None:
    """OpenRouter prices are per-token decimal STRINGS; report $/M tokens."""
    try:
        return float(pricing.get(key)) * 1_000_000
    except (TypeError, ValueError, AttributeError):
        return None


def _model_item(m: dict) -> dict | None:
    """Normalize one catalog row into a menu entry, or None to skip it."""
    mid = str(m.get("id") or "").strip()
    if not mid or mid.startswith(_ALIAS_PREFIX) or mid.endswith(_BATCH_SUFFIX):
        return None
    arch = m.get("architecture") or {}
    outs = arch.get("output_modalities") or []
    ins = arch.get("input_modalities") or []
    if "text" not in outs or "text" not in ins:
        return None  # image/audio generators are not reasoning oracles

    pricing = m.get("pricing") or {}
    inp, out = _price_per_m(pricing, "prompt"), _price_per_m(pricing, "completion")
    reasoning = m.get("reasoning") if isinstance(m.get("reasoning"), dict) else {}
    params = m.get("supported_parameters") or []
    ctx = m.get("context_length") or (m.get("top_provider") or {}).get("context_length")

    cost = "cost unknown"
    if inp is not None and out is not None:
        cost = "free" if inp == 0 and out == 0 else f"${inp:.2f}/${out:.2f} per M"

    tags: list[str] = []
    if reasoning.get("supported_efforts"):
        tags.append("REASONING")
    if inp == 0 and out == 0:
        tags.append("FREE")

    return {
        "model_id": mid,
        "label": str(m.get("name") or mid),
        "provider": mid.split("/", 1)[0] if "/" in mid else "other",
        "description": (str(m.get("description") or "").strip().split("\n")[0])[:200],
        "cost_note": cost,
        "input_per_m": inp,
        "output_per_m": out,
        "context": ctx,
        "supported_efforts": list(reasoning.get("supported_efforts") or []),
        "default_effort": reasoning.get("default_effort"),
        "reasoning_mandatory": bool(reasoning.get("mandatory")),
        "supports_reasoning_effort": "reasoning_effort" in params,
        "created": m.get("created"),
        "tags": tags,
    }


def _score(it: dict, *, cheap: bool) -> float:
    """Rank a model on what the catalog actually tells us.

    Deliberately not a keyword table over model families (that is what atlas.py
    has to do, and it goes stale every release). The signals here are published
    per model, so a model that shipped this morning ranks correctly with no code
    change: does it reason, how much context, how new, and what does it cost."""
    import math

    s = 0.0
    if it["supported_efforts"]:
        s += 3.0  # an engineering oracle wants a reasoner
    ctx = it.get("context") or 0
    if ctx:
        s += min(math.log10(max(ctx, 1)) - 4.0, 2.5)  # 10k -> 0, 1M -> +2
    created = it.get("created") or 0
    if created:
        age_days = max((time.time() - float(created)) / 86400.0, 0.0)
        s += max(2.0 - age_days / 180.0, -1.0)  # fresh models lead, gently
    out = it.get("output_per_m")
    if out is not None and out > 0:
        if cheap:
            s -= (math.log10(out) + 1.0) * 1.6
        else:
            # Price is the only capability proxy this catalog publishes — nobody
            # charges $50/M for a weak model. So when the caller has NOT asked for
            # cheap, price counts FOR a model, not against it. Penalizing it by
            # default is how a default "featured" list for an engineering oracle
            # ends up leading with a tiny free model.
            s += min(math.log10(out) + 1.0, 2.5)
    elif out == 0:
        # Free is a strong signal when asked for and a weak one otherwise — free
        # tiers are real, but they are not what you reach for on a hard question.
        s += 2.0 if cheap else -0.5
    return s


_CHEAP_WORDS = ("cheap", "cheapest", "budget", "fast", "quick", "high-volume", "bulk", "free")


def recommend_models(items: list[dict], task: str, limit: int = 5) -> list[dict]:
    """A provider-diverse shortlist for ``task``, best first."""
    cheap = any(w in (task or "").lower() for w in _CHEAP_WORDS)
    ranked = sorted(items, key=lambda it: _score(it, cheap=cheap), reverse=True)
    out: list[dict] = []
    seen: set[str] = set()
    for it in ranked:  # one per provider first, so the shortlist isn't all one vendor
        if it["provider"] not in seen:
            seen.add(it["provider"])
            out.append(it)
        if len(out) >= limit:
            return out
    for it in ranked:
        if it not in out:
            out.append(it)
        if len(out) >= limit:
            break
    return out


def _featured(items: list[dict], limit: int = 8) -> list[dict]:
    """The shortlist an agent should show first, with no task in hand."""
    return recommend_models(items, "", limit=limit)


def catalog(
    timeout: float = 6.0,
    *,
    task: str = "",
    recommendation_limit: int = 5,
) -> dict:
    """Discover OpenRouter text models and build a menu (best-effort, never raises).

    Same contract as ``atlas.catalog`` so the two handlers stay symmetrical:
    ``{"cloud_ok", "featured", "menu", "effort_choices", "models", "balance_note"}``.
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
            "hint": "could not reach the OpenRouter models endpoint",
        }
    items = [it for it in (_model_item(m) for m in raw.get("data") or []) if it]
    featured = (
        recommend_models(items, task, limit=recommendation_limit) if task else _featured(items)
    )
    return {
        "cloud_ok": True,
        "featured": featured,
        "menu": items,
        "effort_choices": effort_menu(),
        "models": [it["model_id"] for it in items],
        "balance_note": None,
    }


def _model_efforts(model: str, timeout: float = 6.0) -> tuple[list[str], str | None] | None:
    """(supported_efforts, default_effort) for one model, or None if unknown.

    Best-effort and cached for the process: a catalog miss must degrade to
    "send no reasoning_effort", never to a failed turn."""
    if model in _EFFORT_CACHE:
        return _EFFORT_CACHE[model]
    found: tuple[list[str], str | None] | None = None
    cat = catalog(timeout)
    if cat.get("cloud_ok"):
        for it in cat["menu"]:
            _EFFORT_CACHE[it["model_id"]] = (it["supported_efforts"], it["default_effort"])
        found = _EFFORT_CACHE.get(model)
    return found


_EFFORT_CACHE: dict[str, tuple[list[str], str | None]] = {}


# --- the chat call ---------------------------------------------------------


def _clamp_effort(model: str, wanted: str | None) -> str | None:
    """The reasoning effort to actually send, or None to omit the field.

    OpenRouter publishes each model's ``supported_efforts``, so we ask for what
    the model accepts instead of Atlas's approach of sending a guess and retrying
    without it on a 400 — one wasted round trip per call, on every call, to
    rediscover something the catalog already told us."""
    if not wanted:
        return None
    known = _model_efforts(model)
    if known is None:
        return None  # catalog unreachable: omit rather than risk a rejected field
    supported, model_default = known
    if not supported:
        return None  # not a reasoning model
    if wanted in supported:
        return wanted
    # Asked for more than this model offers — take its best, else its own default.
    for fallback in ("xhigh", "high", "medium", "low", "minimal"):
        if fallback in supported:
            return fallback
    return model_default


async def run(
    model: str,
    question: str,
    context: str = "",
    *,
    effort: str | None = None,
    timeout: float | None = None,
) -> OracleResult:
    """Run one turn against an OpenRouter model. Never raises for expected
    failures (network, HTTP, timeout, bad key, missing config)."""
    preset = effort_preset(effort)
    timeout = timeout if timeout is not None else preset["timeout"]
    key = api_key()
    if not configured():
        return OracleResult(
            "error",
            kind="not_configured",
            text="openrouter not configured (set ASK_FABLE_OPENROUTER_API_KEY, or "
            "OPENROUTER_API_KEY which other OpenRouter tooling already uses)",
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
    reasoning_effort = _clamp_effort(model, preset.get("reasoning_effort"))
    metadata: dict = {}
    final_http_status: int | None = None

    def _post() -> tuple[str | None, dict, str | None, int | None]:
        request_body: dict = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": cap,
            "stream": False,
        }
        if reasoning_effort:
            request_body["reasoning"] = {"effort": reasoning_effort}
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {key}",
            "user-agent": USER_AGENT,
            "http-referer": _REFERER,  # OpenRouter attributes the call to the app
            "x-title": _TITLE,
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
        nonlocal final_http_status
        text, _parsed, err, code = _post()
        final_http_status = code
        return text, None, err

    def _telemetry(**over) -> ProviderTelemetry:
        base = {
            "oracle_key": "openrouter",
            "requested_model": model,
            "actual_model": model,
            "transport": "http-json",
            "http_status": final_http_status,
            "wall_duration_ms": (time.perf_counter() - started) * 1000,
            "reasoning_available": False,
            "usage_available": False,
            "tools_available": False,
        }
        return ProviderTelemetry(**{**base, **over})

    started = time.perf_counter()
    try:
        (text, _thinking, err), retry_count = await call_with_transient_retry(
            _call,
            timeout=timeout,
            retryable=lambda r: bool(r[2]) and final_http_status in RETRYABLE_HTTP_STATUSES,
        )
    except TransientRetryTimeout as e:
        return OracleResult(
            "error",
            kind="timeout",
            text=f"{model} timed out after {e.elapsed_s:.0f}s",
            model=model,
            telemetry=_telemetry(http_status=None, retry_count=e.retry_count),
        )
    if err:
        kind, text = http_error_detail(
            label="OpenRouter", error=err, http_status=final_http_status
        )
        return OracleResult(
            "error", kind=kind, text=text, model=model,
            telemetry=_telemetry(retry_count=retry_count),
        )

    shaped = shape(text)
    msg = ((metadata.get("choices") or [{}])[0] or {}).get("message") or {}
    thinking = str(msg.get("reasoning") or "").strip()
    usage_obj = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else None
    usage = None
    if usage_obj:
        details = usage_obj.get("completion_tokens_details") or {}
        prompt_details = usage_obj.get("prompt_tokens_details") or {}
        usage = usage_if_available(
            ProviderUsage(
                input_tokens=token_count(usage_obj.get("prompt_tokens")),
                output_tokens=token_count(usage_obj.get("completion_tokens")),
                total_tokens=token_count(usage_obj.get("total_tokens")),
                cache_read_input_tokens=token_count(prompt_details.get("cached_tokens")),
                cache_creation_input_tokens=token_count(prompt_details.get("cache_write_tokens")),
                # OpenRouter bills the call and tells us what it cost — most
                # backends can only be estimated from a price table.
                cost_usd=normalize_cost_usd(usage_obj.get("cost")),
                cost_basis="billed",  # real money, charged per token
            )
        )
        if details.get("reasoning_tokens"):
            thinking = thinking or f"[{details['reasoning_tokens']} reasoning tokens, not returned]"

    return OracleResult(
        shaped.status,
        text=shaped.text,
        kind=shaped.kind,
        model=model,
        thinking=thinking,
        telemetry=_telemetry(
            # `provider` names the upstream that actually served the call — the
            # same model id can be routed to different providers per request.
            actual_model=(
                f"{metadata.get('model') or model}"
                + (f" (via {metadata['provider']})" if metadata.get("provider") else "")
            ),
            stop_reason=_finish_reason(metadata),
            retry_count=retry_count,
            reasoning_available=bool(thinking),
            usage_available=usage is not None,
            usage=usage,
        ),
    )
