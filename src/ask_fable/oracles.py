"""Oracle registry — one normalized interface over every council backend.

Fable, Fable 5.1, and Opus 5 (OAuth SDK/CLI), MiniMax (`mmx` CLI), Gemini (`agy` CLI), Codex
(`codex` CLI), Grok (`grok` CLI), Kimi (`kimi` CLI), and any Anthropic-compatible HTTP provider
(GLM, DeepSeek, …) each have their own module; this wraps them so the council can
fan out to an arbitrary selection without knowing their internals.

``KNOWN`` is the recognized set and the canonical display order — cheap-first:
the OAuth Anthropic models lead (fable, the default synthesizer, then the pinned
fable51, then opus),
then the cheap direct-API models (deepseek, minimax, glm), then the
subscription-CLI models (gemini, codex, grok, kimi). ``resolve`` reorders every
selection into this order, so it is also the fan-out order.
``default_models()`` is what ``ask_council`` fans out to when the caller names
no models: DEFAULT plus deepseek when its API key is configured. Fable and Opus
ride the same OAuth session, so both are always available; the others are
available only when their bridge/credentials are present (``available``), and
are otherwise reported as a graceful ``not_configured`` error rather than a hard
failure.

``GROUPS`` adds named multi-model tokens on top of that — ``twin`` (the "twin
flames") expands to fable + opus wherever a LIST of models is accepted.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time

from . import (
    ali,
    anthropic_http,
    anthropic_variants,
    atlas,
    cache,
    codex,
    config,
    console,
    fable,
    fable51,
    gemini,
    grok,
    health,
    kimi,
    lmstudio,
    minimax,
    ollama,
    openrouter,
    opus,
    trace_runtime,
    websearch,
)
from .oracle_common import OracleResult  # shared type — lives here to avoid circular imports
from .prompts import SYNTH_SYSTEM_PROMPT
from .provider_telemetry import ProviderTelemetry

KNOWN = (
    "fable",
    "fable51",
    "opus",
    "opus55",
    "opus5",
    "opus48",
    "sonnet",
    "deepseek",
    "minimax",
    "glm",
    "gemini",
    "codex",
    "grok",
    "kimi",
)
DEFAULT = ("fable", "minimax")
SYNTHESIZER = "fable"
TIERS = ("default", "twin", "middle", "full")  # named council presets for the `tier` param

# Named model GROUPS — one operator-facing token that expands to SEVERAL oracles.
# An ALIAS is a second name for ONE model; a group occupies more than one seat, so
# it only means anything where a LIST of models is taken (a council fan-out, a
# chain pipeline). Single-model slots — `synthesizer`, the debate roles — refuse a
# group rather than silently running whichever member happens to sort first.
#
# `twin` (the "twin flames") is the pair that rides the SAME OAuth session as the
# `ask` / `ask_opus5` tools and is therefore always available: the newest Fable
# and Claude Opus 5. Two Anthropic reasoners of different weight and price, no
# extra provider setup, so it is the cheapest useful second opinion there is.
GROUPS: dict[str, tuple[str, ...]] = {
    "twin": ("fable", "opus"),
}

# Extra spellings for the same groups — what an operator actually types. Kept
# explicit rather than derived by a fuzzy normalizer so the tool schema can
# advertise the exact set a strictly-validating MCP host will let through.
GROUP_ALIASES: dict[str, str] = {
    "twins": "twin",
    "twinflame": "twin",
    "twinflames": "twin",
    "twin flame": "twin",
    "twin flames": "twin",
    "twin-flame": "twin",
    "twin-flames": "twin",
    "twin_flame": "twin",
    "twin_flames": "twin",
}

# Claude Agent SDK / `claude` CLI over Claude Code's OAuth session. Keyed to the
# wrapper module so dispatch stays a lookup — a chain of `if key == ...` was
# already awkward at two Anthropic models and does not survive a third.
_ANTHROPIC_BRIDGES = {"fable": fable, "fable51": fable51, "opus": opus, **anthropic_variants.BRIDGES}
_ANTHROPIC = tuple(_ANTHROPIC_BRIDGES)

# Same-lab variants earn a seat when NAMED, never in a blanket fan-out, so the
# tier presets skip them: `fable51`/`opus55`/`opus5` pin what `fable`/`opus`
# already resolve to; `opus48` (previous Opus, for regression/A-B) and `sonnet`
# (a cheap fast Anthropic voice) share Fable's and Opus's training lineage, so
# adding any of them to a council raises the vote count without adding an
# independent opinion (see `lab_of`). Everything else in KNOWN is a distinct-lab
# voice worth its slot.
_TIER_EXCLUDED = ("fable51", "opus55", "opus5", "opus48", "sonnet")
_HTTP = ("glm", "deepseek")  # reached via anthropic_http
# Direct-API oracles that fall back to an Atlas-hosted equivalent when their own
# API key is absent. The direct endpoint is cheaper and stays preferred; this only
# keeps the oracle alive on one Atlas key instead of reporting it unavailable.
_ATLAS_FALLBACK = {"glm": "zai-org/glm-5.3"}
OLLAMA_PREFIX = "ollama:"  # dynamic tokens: ollama:<model>, model rides in the key
LMS_PREFIX = "lmstudio:"  # dynamic tokens: lmstudio:<model> on the configured LM Studio server
ATLAS_PREFIX = "atlas:"  # dynamic tokens: atlas:<model> (OpenAI-compatible Atlas Cloud chat)
OPENROUTER_PREFIX = "openrouter:"  # dynamic tokens: openrouter:<model> (~400 models, one key)
ALI_PREFIX = "ali:"  # dynamic tokens: ali:<model> (Alibaba/Qwen MaaS, Anthropic wire)


def ollama_model(key: str) -> str | None:
    """The Ollama model carried by an ``ollama:<model>`` token, or None."""
    if key.startswith(OLLAMA_PREFIX):
        return key[len(OLLAMA_PREFIX) :].strip() or None
    return None


def lmstudio_model(key: str) -> str | None:
    """The LM Studio model carried by an ``lmstudio:<model>`` token, or None."""
    if key.startswith(LMS_PREFIX):
        return key[len(LMS_PREFIX) :].strip() or None
    return None


def atlas_model(key: str) -> str | None:
    """The Atlas Cloud model carried by an ``atlas:<model>`` token, or None."""
    if key.startswith(ATLAS_PREFIX):
        return key[len(ATLAS_PREFIX) :].strip() or None
    return None


def openrouter_model(key: str) -> str | None:
    """The OpenRouter model carried by an ``openrouter:<model>`` token, or None."""
    if key.startswith(OPENROUTER_PREFIX):
        return key[len(OPENROUTER_PREFIX) :].strip() or None
    return None


def ali_model(key: str) -> str | None:
    """The Alibaba/Qwen model carried by an ``ali:<model>`` token, or None."""
    if key.startswith(ALI_PREFIX):
        return key[len(ALI_PREFIX) :].strip() or None
    return None


def group_members(token: str) -> tuple[str, ...] | None:
    """The oracles a group token expands to, or None when it names no group.

    Case- and spelling-tolerant across the registered spellings: ``twin``,
    ``twins``, ``Twin Flames`` and ``twin_flame`` all name the same pair."""
    tok = str(token).strip().lower()
    return GROUPS.get(GROUP_ALIASES.get(tok, tok))


def expand_groups(models: list) -> list[str]:
    """Replace every group token with its members, in place, preserving order.

    Runs BEFORE ``_canon``/``ALIASES`` so a group behaves exactly as if the
    operator had typed its members: ``['twin', 'minimax']`` is
    ``['fable', 'opus', 'minimax']``, and the caller's own de-dupe and ordering
    rules then apply unchanged."""
    out: list[str] = []
    for m in models:
        members = group_members(m)
        out.extend(members if members is not None else [str(m)])
    return out


def label(key: str) -> str:
    """Human/model label for a key, without needing the oracle to have run."""
    if key == "fable":
        # Deliberately dynamic — `fable` is a ladder, not an id. It moves at most
        # once per process (a demotion is one-way), so the answer cache keyed on
        # this loses at most one generation of entries when that happens.
        return fable.fable_model()
    if key == "fable51":
        return fable51.FABLE51_MODEL
    if key == "opus":
        # Dynamic — `opus` is a ladder like `fable`, not a fixed id.
        return opus.opus_model()
    if key in anthropic_variants.SPECS:
        return anthropic_variants.SPECS[key].model
    if key == "minimax":
        return minimax.minimax_model()
    if key == "gemini":
        return gemini.gemini_model()
    if key == "codex":
        return codex.codex_model()
    if key == "grok":
        return grok.grok_model()
    if key == "kimi":
        return kimi.local_model_for(kimi.kimi_model())
    if key in _HTTP:
        cfg = anthropic_http.config_for(key)
        if cfg:
            return cfg.model
        return _ATLAS_FALLBACK.get(key) or key
    m = ollama_model(key)
    if m:
        return m  # display the bare model, e.g. kimi-k2.7-code:cloud
    lm = lmstudio_model(key)
    if lm:
        return lm  # display the bare model, e.g. qwen3.8-27b-distill-q38
    am = atlas_model(key)
    if am:
        return am  # display the bare model, e.g. xai/grok-4.6
    om = openrouter_model(key)
    if om:
        return om  # display the bare model, e.g. anthropic/claude-fable-5.1
    lim = ali_model(key)
    if lim:
        return lim  # display the bare model, e.g. qwen3.8-max
    return key


# Training-lineage families. A council's quorum/consensus signal is an
# INDEPENDENCE claim: two members from the same lab share pretraining and RLHF, so
# their errors correlate, and counting both as agreement overstates the evidence.
# `distinct_labs` lets the council DOWNGRADE a unanimous verdict that spans only
# one lab (four Anthropic voices are one opinion, not four). This is also what
# makes the same-lab `_TIER_EXCLUDED` variants safe to name in an explicit council.
_LAB: dict[str, str] = {
    "fable": "anthropic",
    "fable51": "anthropic",
    "opus": "anthropic",
    "opus55": "anthropic",
    "opus5": "anthropic",
    "opus48": "anthropic",
    "sonnet": "anthropic",
    "minimax": "minimax",
    "deepseek": "deepseek",
    "glm": "zhipu",
    "gemini": "google",
    "codex": "openai",
    "grok": "xai",
    "kimi": "moonshot",
}

# Substrings that identify a lab inside a dynamic gateway token's model id
# (atlas:/openrouter:/ollama:/lmstudio:), longest-intent first. Only ever used to
# MERGE a token into a known family so a single lab can't masquerade as several;
# an id matching nothing here stays its own lab (see `lab_of`).
_LAB_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("claude", "anthropic"), ("fable", "anthropic"), ("opus", "anthropic"),
    ("sonnet", "anthropic"), ("haiku", "anthropic"),
    ("gpt", "openai"), ("codex", "openai"), ("o1-", "openai"), ("o3-", "openai"),
    ("gemini", "google"), ("gemma", "google"),
    ("deepseek", "deepseek"), ("glm", "zhipu"), ("grok", "xai"),
    ("kimi", "moonshot"), ("moonshot", "moonshot"), ("qwen", "alibaba"),
    ("minimax", "minimax"), ("llama", "meta"), ("mistral", "mistral"),
    ("nemotron", "nvidia"),
)


def lab_of(key: str) -> str:
    """Best-effort training-lineage family for a model token.

    KNOWN keys map exactly; a dynamic gateway token (``atlas:``/``openrouter:``/
    ``ollama:``/``lmstudio:``) is matched by substring against its model id, and an
    unrecognized token falls back to ITSELF — so an unknown provider counts as its
    own lab and is never silently merged into another. The lab-diversity check only
    ever DOWNGRADES a strong verdict, so the failure mode of a miss is a missed
    downgrade (status quo), never a fabricated claim of independence."""
    if key in _LAB:
        return _LAB[key]
    mid = (
        ollama_model(key)
        or lmstudio_model(key)
        or atlas_model(key)
        or openrouter_model(key)
        or ali_model(key)
        or key
    ).lower()
    for needle, lab in _LAB_SUBSTRINGS:
        if needle in mid:
            return lab
    return key


def distinct_labs(keys) -> int:
    """How many distinct training-lineage families a set of oracle keys spans."""
    return len({lab_of(k) for k in keys})


def default_models() -> list[str]:
    """DEFAULT plus deepseek when its API key is configured (cheap-first preference).

    Checked at call time so setting/unsetting ASK_FABLE_DEEPSEEK_API_KEY takes
    effect without a restart."""
    return ["fable", "deepseek", "minimax"] if available("deepseek") else list(DEFAULT)


# Operator denylist. A token here is dropped from every fan-out and refused by
# its dedicated tool with a clear "disabled" message — distinct from
# "not configured", because the operator turned it OFF on purpose, not because a
# key is missing. Config file wins over env, so `configure_disabled` toggles it at
# runtime with no restart.
DISABLED_KEY = "ASK_FABLE_DISABLED"

# Coarse provider names a denylist entry may name to disable a whole gateway at
# once (every `atlas:<model>` token, say), rather than a single oracle key.
_PROVIDER_PREFIXES: tuple[tuple[str, str], ...] = (
    (ATLAS_PREFIX, "atlas"),
    (OPENROUTER_PREFIX, "openrouter"),
    (OLLAMA_PREFIX, "ollama"),
    (LMS_PREFIX, "lmstudio"),
    (ALI_PREFIX, "ali"),
)


def provider_of(key: str) -> str:
    """The coarse provider name for a token — the granularity a denylist entry
    like ``atlas`` matches at. A dynamic ``atlas:``/``openrouter:``/``ollama:``/
    ``lmstudio:`` token maps to its gateway; every other key is its own provider."""
    low = key.lower()
    for prefix, name in _PROVIDER_PREFIXES:
        if low.startswith(prefix):
            return name
    return low


def disabled_tokens() -> frozenset[str]:
    """The operator's denylist, normalized: oracle keys (aliases folded, so ``m3``
    and ``minimax`` both count) and coarse provider names (``atlas`` …).

    Config file wins over env (``configure_disabled`` writes the file); either may
    be a JSON array or a comma/space-separated string. Empty when nothing is set.
    An explicit empty config array wins too: it is how re-enabling the last entry of
    an env denylist sticks, instead of handing the list straight back to the env."""
    raw = config.get_list(DISABLED_KEY, keep_empty=True)
    if raw is None:
        env = (os.environ.get(DISABLED_KEY) or "").strip()
        raw = [p for p in env.replace(",", " ").split() if p] if env else []
    out: set[str] = set()
    for tok in raw:
        t = str(tok).strip().lower()
        if t:
            out.add(ALIASES.get(t, t))  # fold m3->minimax; provider names pass through
    return frozenset(out)


def is_disabled(key: str) -> bool:
    """True when ``key`` — or the provider it belongs to — is on the operator's
    denylist. Matches the oracle key, its alias-canonical form, and its coarse
    provider name, so ``atlas`` disables every ``atlas:<model>`` token."""
    denied = disabled_tokens()
    if not denied:
        return False
    low = key.lower()
    return (
        low in denied
        or ALIASES.get(low, low) in denied
        or provider_of(key) in denied
    )


def available(key: str) -> bool:
    """True when this oracle can actually be reached right now."""
    if is_disabled(key):
        return False  # operator turned it off (ASK_FABLE_DISABLED / configure_disabled)
    if key in _ANTHROPIC:
        # Claude Code — the SDK or the `claude` CLI — or, when the operator has
        # explicitly pinned the API transport, a configured key. Presence, not
        # health: an unauthenticated CLI still reports present, and its auth error
        # surfaces per call rather than silently re-routing (see `fable`).
        return fable.claude_code_present() or (
            fable.http_transport_selected() and bool(fable.http_api_key())
        )
    if key == "minimax":
        return shutil.which("mmx") is not None
    if key == "gemini":
        return shutil.which(gemini.GEMINI_BINARY) is not None
    if key == "codex":
        return shutil.which(codex.CODEX_BINARY) is not None
    if key == "grok":
        return grok.available()
    if key == "kimi":
        return kimi.available()
    if key in _HTTP:
        if anthropic_http.config_for(key) is not None:
            return True
        return key in _ATLAS_FALLBACK and atlas.configured()
    if ollama_model(key):
        return ollama.configured()  # local daemon (signin) or a remote key
    if lmstudio_model(key):
        return lmstudio.configured()  # a LAN server; reachability is a per-call error
    if atlas_model(key):
        return atlas.configured()  # needs an API key for the chat endpoint
    if openrouter_model(key):
        return openrouter.configured()  # ditto — the catalog is free, chat is not
    if ali_model(key):
        return ali.configured()  # Alibaba/Qwen MaaS — needs an API key
    return False


def disabled_result(key: str, model: str = "") -> OracleResult:
    """The fail-closed result for a backend on the operator's denylist. Its own
    ``disabled`` kind, so a denylisted single-model tool says so plainly and a
    council reports it as skipped rather than as a transport error."""
    return OracleResult(
        "error",
        key=key,
        kind="disabled",
        model=model or label(key),
        text=(
            f"{label(key)} is disabled by the operator ({DISABLED_KEY}); "
            "re-enable it with the configure_disabled tool or by editing the denylist"
        ),
    )


def placeholder_telemetry(result: OracleResult) -> bool:
    """True when ``result`` carries no real telemetry: none at all, or the
    ``transport="error"`` stub ``OracleResult.__post_init__`` attaches to every
    error result so the field is never missing. Either way the call has not been
    recorded anywhere yet."""
    return result.telemetry is None or result.telemetry.transport == "error"


def fallback_telemetry(
    *,
    key: str,
    requested_model: str,
    wall_duration_ms: float,
    actual_model: str = "",
    returncode: int | None = None,
    thinking: str = "",
    transport: str = "",
) -> ProviderTelemetry:
    """Telemetry for an outcome that carries none — a bridge that predates the
    field, or a synthetic result (breaker skip, cancellation, leaked exception).
    Attaching one means the outcome still lands as a ``provider.completed`` event,
    so it counts in ``stats(by="provider")`` instead of vanishing.

    Takes fields rather than an ``OracleResult`` so a caller with no result to
    hand — a cancelled call — need not build a throwaway one (whose own
    ``__post_init__`` would allocate a second telemetry object to discard).

    ``transport`` defaults to how this oracle is actually reached, so an
    Anthropic-backed key reports ``sdk`` here exactly as the hand-built telemetry
    on the direct ``ask`` path does. A caller that knows better — a skip that
    reached no backend at all — passes its own."""
    return ProviderTelemetry(
        oracle_key=key,
        requested_model=requested_model,
        actual_model=actual_model or requested_model,
        transport=transport or ("sdk" if key in _ANTHROPIC else "bridge"),
        returncode=returncode,
        wall_duration_ms=wall_duration_ms,
        reasoning_available=bool(thinking),
        usage_available=None if key == "gemini" else False,
        tools_available=(
            True
            if key == "codex"
            else False
            if key in ("grok", "kimi")
            else None
            if key == "gemini"
            else False
        ),
    )


async def run(
    key: str,
    question: str,
    context: str = "",
    *,
    effort: str | None = None,
    model: str | None = None,
) -> OracleResult:
    started = time.perf_counter()
    # Resolved once and passed down: resolving again inside _run dispatches
    # through the model ladder a second time, and if it demotes between the two
    # the same call reports two different model names.
    requested = model or label(key)
    if is_disabled(key):  # the operator turned this backend off — fail closed
        return disabled_result(key, requested)
    if key == "websearch":
        # The opt-in web-search router is deliberately UNCACHED: web results are
        # time-sensitive and non-idempotent, so a cached "latest ..." answer is a
        # stale lie. The circuit breaker is also skipped — it exists to shed load
        # from chronically-failing COUNCIL backends, not this user-invoked tool.
        result = await _run_uncached(key, question, context, effort=effort, model=model)
    else:
        result = await _run(key, question, context, effort=effort, model=model, model_name=requested)
    if placeholder_telemetry(result):
        result.telemetry = fallback_telemetry(
            key=key,
            requested_model=requested,
            wall_duration_ms=(time.perf_counter() - started) * 1000,
            actual_model=result.model,
            returncode=result.returncode,
            thinking=result.thinking,
        )
    trace_runtime.record_provider(
        result.telemetry,
        result.status,
        result.thinking,
        kind=result.kind,
        answer=result.text if result.status == "ok" else None,
    )
    return result


async def run_bounded(
    sem: asyncio.Semaphore, key: str, question: str, context: str = ""
) -> OracleResult:
    """``run`` behind a concurrency limiter, for a fan-out that caps how many
    bridges it opens at once.

    The limiter is here rather than in the orchestrator because the wait is part
    of what has to be captured: a member cancelled while still QUEUED never
    enters ``run``, so the capture in there cannot speak for it. Keeping both
    cases in one place is what stops a future fan-out from silently re-opening
    the gap — and means the queued case is recorded as what it is, a call that
    reached no backend at all, rather than a bridge call that took as long as
    the queue wait."""
    entered = False
    try:
        async with sem:
            entered = True
            return await run(key, question, context)
    except asyncio.CancelledError:
        if not entered:
            # Never got a slot, so no backend was touched: zero duration and the
            # same ``skipped`` transport a breaker skip reports, or stats would
            # charge this oracle a latency sample and an error for a call it
            # never received.
            _record_unfinished(key, label(key), 0.0, "cancelled", transport="skipped")
        raise


def _record_unfinished(
    key: str, requested: str, started: float, kind: str, *, transport: str = ""
) -> None:
    """Emit a provider event for a call that ended without a result. Council,
    chain and debate all cap themselves by cancelling an in-flight call, so doing
    this here — rather than in each orchestrator — is what makes "every attempted
    oracle leaves a provider.completed" true for all three.

    NB the vocabulary: this is what the ORACLE saw, so a capped member is
    ``cancelled`` here while the orchestrator reports the same member as
    ``timeout`` in its ``sources`` — the oracle cannot know whether it was a
    council cap, a chain cap or a client disconnect that stopped it."""
    trace_runtime.record_provider(
        fallback_telemetry(
            key=key,
            requested_model=requested,
            wall_duration_ms=(time.perf_counter() - started) * 1000 if started else 0.0,
            transport=transport,
        ),
        "error",
        kind=kind,
    )


async def run_synthesis(key: str, prompt: str, *, on_think=None) -> OracleResult:
    """One synthesis turn on any oracle backend (the council's reconciliation call).

    Deliberate direct dispatch: no answer cache (every synthesis prompt embeds a
    unique panel of answers, so a hit is impossible) and no circuit breaker (a
    failed synthesis already has its own fallback ladder inside the council). For
    fable the SYNTH prompt rides the system channel; every other backend gets it
    folded into the message, atop the backend's own scope prompt.

    Direct dispatch also skips ``run``'s denylist gate, so it is applied here: a
    synthesizer the operator disabled (Fable, by default) is refused, not called."""
    if is_disabled(key):
        return disabled_result(key)
    started = time.perf_counter()
    result = await _run_uncached(key, prompt, system_prompt=SYNTH_SYSTEM_PROMPT, on_think=on_think)
    if placeholder_telemetry(result):
        result.telemetry = fallback_telemetry(
            key=key,
            requested_model=label(key),
            wall_duration_ms=(time.perf_counter() - started) * 1000,
            actual_model=result.model,
            returncode=result.returncode,
            thinking=result.thinking,
        )
    return result


async def _run(
    key: str,
    question: str,
    context: str = "",
    *,
    effort: str | None = None,
    model: str | None = None,
    model_name: str,
) -> OracleResult:
    """Run one oracle, returning a normalized result (never raises).

    The answer cache is consulted FIRST — a hit needs no backend call, so an open
    circuit breaker must not gate it (the breaker's job is to shed load from a
    struggling backend; the cache path generates none). Only a miss checks the
    breaker: if the oracle is ``open`` (chronically failing), a synthetic
    ``circuit_open`` result is returned instead of making the call. Every real
    (uncached) outcome is recorded so the breaker tracks actual backend health.

    The key names the ORACLE, not just the model it resolves to: ``label`` drops
    the gateway prefix, so ``atlas:openai/gpt-5.6-sol`` and
    ``openrouter:openai/gpt-5.6-sol`` would otherwise share one entry (one
    gateway's answer served as the other's), as would ``fable`` and the pinned
    ``fable51`` while both resolve to 5.1."""
    ck = cache.key(
        f"oracle:{key}", [model_name], question, context, effort=cache_effort(key, effort)
    )
    cached = cache.get(ck)
    if cached is not None:
        payload, age_s = cached
        trace_runtime.record_stage(
            "cache.inner",
            "hit",
            kind=trace_runtime.EventKind.CACHE,
            cache={
                "status": "hit",
                "layer": "oracle",
                "age_ms": age_s * 1000,
                "source_trace_id": payload.get("origin_trace_id"),
            },
        )
        status = payload.get("status")
        if status == "ok":
            return OracleResult(
                key=key,
                status=status,
                text=payload.get("text", ""),
                kind=payload.get("kind", ""),
                model=payload.get("model", model_name),
                thinking=payload.get("thinking", ""),
                telemetry=ProviderTelemetry(
                    oracle_key=key,
                    requested_model=model_name,
                    actual_model=payload.get("model", model_name),
                    transport="cache",
                    wall_duration_ms=0.0,
                ),
            )
    trace_runtime.record_stage(
        "cache.inner",
        "miss",
        kind=trace_runtime.EventKind.CACHE,
        cache={"status": "miss", "layer": "oracle"},
    )

    # Circuit breaker: skip chronically-failing oracles (cache misses only). A
    # quota hold rides the same skip path but is a different reason — word it so —
    # while keeping kind=circuit_open (both are shed calls; no new kind to thread
    # through _NON_HEALTH_KINDS and every enumeration).
    if health.breaker.should_skip(key):
        gate = health.breaker.snapshot(key)
        if gate["skip_reason"] == "quota_hold":
            remaining = gate["resume_in_s"] or 0
            text = f"{key} is rate-limited (quota hold, ~{remaining:.0f}s remaining)"
        else:
            text = f"{key} circuit breaker is open (recent error rate exceeded threshold)"
        skipped = OracleResult(
            "error",
            key=key,
            kind="circuit_open",
            text=text,
            model=model_name,
        )
        # No backend was reached, and only this line knows that — labelling it
        # like a real call would leave the error kind as the sole way to tell a
        # shed call from one that actually ran.
        skipped.telemetry = fallback_telemetry(
            key=key,
            requested_model=model_name,
            wall_duration_ms=0.0,
            transport="skipped",
        )
        return skipped

    # Only the backend call is wrapped. A failure in the bookkeeping BELOW —
    # breaker, cache write — must not be reported as a failed attempt for a call
    # the oracle actually answered.
    call_started = time.perf_counter()
    try:
        result = await _run_uncached(key, question, context, effort=effort, model=model)
    except asyncio.CancelledError:
        # A caller's wall-clock cap cancelled us mid-flight — a council fan-out,
        # a chain or debate pipeline. The attempt still happened, so record it
        # under this oracle's own key before propagating: otherwise the member
        # that ran LONG is the one member missing from stats(by="provider"),
        # which is the exact failure that view exists to catch. Deliberately not
        # fed to the circuit breaker — our impatience is not evidence of backend
        # ill-health.
        _record_unfinished(key, model_name, call_started, "cancelled")
        raise
    except Exception:
        # A bridge is contracted never to raise; one that does still made a real
        # call, and the orchestrator turns it into an ``sdk_error`` source.
        _record_unfinished(key, model_name, call_started, "sdk_error")
        raise
    try:
        transition = health.breaker.record(key, result.status, result.kind)
        if transition is not None:
            _report_breaker(transition)
    except Exception as exc:  # noqa: BLE001 — bookkeeping never costs an answer
        print(f"ask_fable: breaker bookkeeping failed: {exc}", file=sys.stderr)

    # Only cache successes. Refusals are often transient/nondeterministic (safety-filter
    # flakiness, provider hiccups, an over-eager refusal classifier — cf. the "payload"
    # false-positive) — pinning one for the full TTL would degrade every later council
    # for that (question, context) for an hour, so re-ask instead. A result the bridge
    # flagged as TRUNCATED is likewise never pinned: a cached truncated answer is a
    # permanent lie. So is an answer from a different model than the key names: a
    # laddered `fable`/`opus` call demoted mid-flight (5.1 rejected, 5 answered)
    # would be pinned under the 5.1 label and later served as 5.1. Only the
    # Anthropic bridges report the served id that way — elsewhere `model` may
    # legitimately differ from the label (a gateway id served by a local CLI alias).
    served_as_named = key not in _ANTHROPIC or not result.model or result.model == model_name
    if result.status == "ok" and result.kind != "truncated" and served_as_named:
        payload = {
            "status": result.status,
            "text": result.text,
            "kind": result.kind,
            "model": result.model,
            "thinking": result.thinking,
        }
        cache.put(ck, trace_runtime.prepare_cache_store(payload))
    return result


def cache_effort(key: str, effort: str | None) -> str | None:
    """The effort the answer cache keys a call on.

    An explicit effort is keyed as given. ``None`` means "the operator's default",
    which lives in config/env and can change between calls — and between
    processes, since the cache outlives both — so for a backend that takes an
    effort it is resolved to the defaults the call would actually run at;
    otherwise an answer computed at the old default keeps being served after the
    operator changed it. A gateway token may be answered by the local grok/kimi
    CLI instead, so every default that could apply goes in."""
    takes_effort = (
        key in ("grok", "kimi", "codex")
        or key in _ATLAS_FALLBACK
        or bool(atlas_model(key) or openrouter_model(key))
    )
    if effort or not takes_effort:
        return effort
    defaults = (
        atlas.default_effort(),
        openrouter.default_effort(),
        grok.grok_reasoning(),
        kimi.kimi_effort(),
        # codex sends `-c model_reasoning_effort=<this>` on every call, so an
        # answer computed at the old default was served after the operator
        # changed it — the same staleness the others are keyed against.
        codex.codex_reasoning(),
    )
    return "default:" + "/".join(defaults)


def panel_cache_effort(keys: list[str], effort: str | None = None) -> str | None:
    """The effort term for a MULTI-model cache key (council / chain / debate).

    Those panels run their members with ``effort=None``, so the answer depends on
    whatever defaults the operator has configured — and the outer tool cache
    outlives the process that resolved them. Without this the key carried no
    effort at all and a council answered at the old default kept being served."""
    if effort:
        return effort
    for key in keys:
        term = cache_effort(key, None)
        if term:
            return term
    return None


def _report_breaker(t: health.Transition) -> None:
    """Make a breaker state change visible. Until now the only symptom of a trip
    was ``circuit_open`` in a council's ``sources`` — the transition itself left
    no line on the console and no event in the log, so "why was GLM skipped all
    afternoon?" had nothing to point at. One stderr line for whoever is watching
    live; one trace event, carrying a ``provider`` block so
    ``trace_list(provider=...)`` and ``rg breaker decisions.jsonl`` both find it."""
    who = label(t.key)
    if t.to == "closed":
        why = "probe succeeded" if t.probe else "a call already in flight succeeded"
        console.notice(f"circuit breaker closed for {who} — {why}", tone="ok")
    else:
        console.notice(
            f"circuit breaker {t.to} for {who}: {t.error_rate:.0%} errors over "
            f"{t.samples}/{t.window} calls; skipping it for {t.cooldown_s:.0f}s"
        )
    trace_runtime.record_event(
        f"breaker.{t.to}",
        trace_runtime.EventKind.SERVER,
        t.to,
        provider={"oracle_key": t.key, "actual_model": who},
        orchestration={
            "breaker": {
                "error_rate": t.error_rate,
                "samples": t.samples,
                "window": t.window,
                "cooldown_s": t.cooldown_s,
            }
        },
    )


async def _run_uncached(
    key: str,
    question: str,
    context: str = "",
    *,
    effort: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    on_think=None,
) -> OracleResult:
    """Helper to run the actual oracle call without cache checking.

    Each bridge now returns ``OracleResult`` directly; we just stamp the ``key``
    so the council/chain dispatch can identify which oracle produced which result.
    ``model`` is an optional bridge override (currently honored by ``grok``).
    ``system_prompt`` overrides the Anthropic bridges' system channel; every other
    bridge owns its system channel (ORACLE_SYSTEM_PROMPT), so the override is folded
    into the message instead — the same fold codex applies to its own scope prompt.
    ``on_think`` streams reasoning live and is honored only by fable/opus (the
    Claude Agent SDK is the only bridge that emits reasoning incrementally)."""
    if key == "websearch":
        # Opt-in research router: dispatches to grok or a Claude bridge with live
        # web search ON (the one tool that browses). ``model`` selects the backend.
        r = await websearch.run(question, context, effort=effort, model=model)
        r.key = key
        return r
    if key in _ANTHROPIC:
        bridge = _ANTHROPIC_BRIDGES[key]
        r = await bridge.run(
            question,
            context,
            system_prompt=system_prompt,
            **({"on_think": on_think} if on_think else {}),
        )
        r.key = key
        if not r.model:
            r.model = label(key)
        return r
    if system_prompt:
        question = f"{system_prompt}\n\n{question}"
    if key == "minimax":
        r = await minimax.run(question, context)
        r.key = key
        return r
    if key == "gemini":
        r = await gemini.run(question, context)
        r.key = key
        return r
    if key == "codex":
        r = await codex.run(question, context)
        r.key = key
        return r
    if key == "grok":
        r = await grok.run(question, context, effort=effort, model=model)
        r.key = key
        return r
    if key == "kimi":
        r = await kimi.run(question, context, effort=effort, model=model)
        r.key = key
        return r
    if key in _HTTP:
        cfg = anthropic_http.config_for(key)
        if cfg is None:
            fallback = _ATLAS_FALLBACK.get(key)
            if fallback and atlas.configured() and not is_disabled("atlas"):
                r = await atlas.run(fallback, question, context, effort=effort)
                r.key = key  # attribution keeps the oracle key, not the atlas token
                return r
            return OracleResult(
                "error",
                key=key,
                text=f"{key} not configured (set ASK_FABLE_{key.upper()}_API_KEY)",
                kind="not_configured",
                model=label(key),
            )
        r = await anthropic_http.run(cfg, question, context)
        r.key = key
        return r
    model = ollama_model(key)
    if model:
        if not ollama.configured():
            return OracleResult(
                "error",
                key=key,
                text="ollama not reachable (default is a local "
                "`ollama serve` + `ollama signin`; or set ASK_FABLE_OLLAMA_API_KEY "
                "for ollama.com)",
                kind="not_configured",
                model=model,
            )
        r = await ollama.run(model, question, context)
        r.key = key
        return r
    lmodel = lmstudio_model(key)
    if lmodel:
        if not lmstudio.configured():
            return OracleResult(
                "error",
                key=key,
                text="lmstudio not configured (set ASK_FABLE_LMSTUDIO_BASE_URL)",
                kind="not_configured",
                model=lmodel,
            )
        r = await lmstudio.run(lmodel, question, context)
        r.key = key
        return r
    amodel = atlas_model(key)
    if amodel:
        # Prefer the operator's local `grok` / `kimi` CLI for those model families
        # while it can carry the call — same model, their existing login, no
        # per-token Atlas billing (see _via_local_cli).
        r = await _via_local_cli(
            amodel, question, context, effort, gateway_ready=atlas.configured()
        )
        if r is not None:
            r.key = key  # keep the atlas: token so sources stay attributable
            return r
        if not atlas.configured():
            return OracleResult(
                "error",
                key=key,
                text="atlas not configured (set "
                "ASK_FABLE_ATLAS_API_KEY, or ATLASCLOUD_API_KEY which the Atlas "
                "Cloud MCP server already uses)",
                kind="not_configured",
                model=amodel,
            )
        r = await atlas.run(amodel, question, context, effort=effort)
        r.key = key
        return r
    omodel = openrouter_model(key)
    if omodel:
        # Same prefer-local rule as Atlas: a Grok or Kimi id the operator can
        # already serve from an authenticated CLI should not be billed per token
        # by a gateway. Attribution keeps the openrouter: token either way.
        r = await _via_local_cli(
            omodel, question, context, effort, gateway_ready=openrouter.configured()
        )
        if r is not None:
            r.key = key
            return r
        if not openrouter.configured():
            return OracleResult(
                "error",
                key=key,
                text="openrouter not configured (set "
                "ASK_FABLE_OPENROUTER_API_KEY, or OPENROUTER_API_KEY which "
                "other OpenRouter tooling already uses)",
                kind="not_configured",
                model=omodel,
            )
        r = await openrouter.run(omodel, question, context, effort=effort)
        r.key = key
        return r
    limodel = ali_model(key)
    if limodel:
        if not ali.configured():
            return OracleResult(
                "error",
                key=key,
                text="ali not configured (set ASK_FABLE_ALI_API_KEY)",
                kind="not_configured",
                model=limodel,
            )
        # Like the _HTTP bridges: a synthesis system_prompt is already folded into
        # `question` above, so ali.run keeps its default ORACLE_SYSTEM_PROMPT scope.
        r = await ali.run(limodel, question, context)
        r.key = key
        return r
    return OracleResult(
        "error", key=key, text=f"unknown oracle: {key}", kind="unknown_oracle", model=key
    )


# What a local CLI answers when it cannot take a call AT ALL: the prompt is too big
# for its one argv value, or it has no config to build its sandbox from.
_LOCAL_CLI_UNUSABLE = ("context_too_large", "not_configured")


async def _via_local_cli(
    gateway_model: str,
    question: str,
    context: str,
    effort: str | None,
    *,
    gateway_ready: bool,
) -> OracleResult | None:
    """Serve a gateway Grok/Kimi id from the operator's own CLI, or None when the
    gateway should answer instead.

    The CLI is the same model family on a login the operator already pays for, so
    it is preferred — but only while it can carry the call. Both CLIs take the
    prompt as ONE argv value (~120 KB), far below the models' own context, so a
    prompt that does not fit goes to the gateway: refusing it locally with advice
    to "use the gateway" would route that advice straight back here. The same
    holds when the local CLI turns out unusable (``_LOCAL_CLI_UNUSABLE``). Without
    a configured gateway the local answer — including its error, which names the
    fix — is returned as is."""
    if (
        grok.looks_like_grok_model(gateway_model)
        and grok.available()
        # `available()` already refuses a disabled backend, but the reroute is
        # reached under the GATEWAY's key (`atlas:xai/grok-4.6` -> provider
        # `atlas`), so `run()`'s denylist check never saw `grok`/`kimi`. Without
        # this the operator turning grok off still spawned the grok CLI, and the
        # answer came back attributed to the atlas token. Capability is not
        # authorization: fall through to the gateway instead.
        and not is_disabled("grok")
    ):
        fits, bridge = grok.prompt_fits(question, context), grok
    # A Kimi id needs a KNOWN local alias — an unmappable one stays on the gateway
    # rather than silently answering as the default local model under its name.
    elif (
        kimi.available()
        and kimi.local_alias_for(gateway_model) is not None
        and not is_disabled("kimi")
    ):
        fits, bridge = kimi.prompt_fits(question, context), kimi
    else:
        return None
    if not fits and gateway_ready:
        return None
    r = await bridge.run(question, context, model=gateway_model, effort=effort)
    if r.kind in _LOCAL_CLI_UNUSABLE and gateway_ready:
        return None
    return r


def tier_models(tier: str) -> list[str]:
    """Expand a named council preset into a model-token list.

    - ``default`` → fable + minimax, plus deepseek when ASK_FABLE_DEEPSEEK_API_KEY is set
    - ``twin``    → the twin flames, fable + opus (any ``GROUPS`` name works here)
    - ``middle``  → all of KNOWN except ``_TIER_EXCLUDED``, cheap-first (fable,
      opus, deepseek, minimax, glm, gemini, codex, grok, kimi)
    - ``full``    → + the configured Ollama Cloud models (``ASK_FABLE_OLLAMA_COUNCIL``)

    Middle/full list every KNOWN member unconditionally — unconfigured ones
    (glm/deepseek/ollama without a key) are reported and skipped at run time,
    exactly as with an explicit ``models`` list. An unrecognized tier falls back
    to ``default``."""
    tier = (tier or "").strip().lower()
    group = group_members(tier)
    if group is not None:
        return list(group)
    members = [k for k in KNOWN if k not in _TIER_EXCLUDED]
    if tier == "middle":
        return members
    if tier == "full":
        return members + [OLLAMA_PREFIX + m for m in ollama.council_models()]
    return default_models()


# Operator-friendly aliases accepted by the sequential chain (the single-model
# tool is `ask_m3`, but the oracle key is `minimax`), resolved before matching.
ALIASES = {
    "m3": "minimax",
    "gpt": "codex",
    "xai": "grok",
    # `opus` is the LADDER (newest Opus). `opus5`/`opus55` NAME a specific version
    # — so `opus-5`/`claude-opus-5` resolve to the pin, not the ladder. (Bare
    # `opus5`/`opus55` are KNOWN keys and need no alias.)
    "opus-5": "opus5",
    "claude-opus-5": "opus5",
    "opus5.5": "opus55",
    "opus-5.5": "opus55",
    "opus-55": "opus55",
    "claude-opus-5-5": "opus55",
    "opus4.8": "opus48",
    "opus-4.8": "opus48",
    "opus-48": "opus48",
    "claude-opus-4-8": "opus48",
    "sonnet5": "sonnet",
    "sonnet-5": "sonnet",
    "claude-sonnet-5": "sonnet",
    "fable5.1": "fable51",
    "fable-5.1": "fable51",
    "fable-51": "fable51",
    "claude-fable-5-1": "fable51",
}


def _canon(tok: str) -> str:
    """Canonicalize one requested model token. Names, aliases, and the
    ``ollama:``/``atlas:`` prefixes are case-insensitive, but an Atlas model id
    keeps its casing verbatim — Atlas ids are case-SENSITIVE (e.g.
    ``deepseek-ai/DeepSeek-V3.1-Terminus``), so lowercasing one silently turns a
    valid model into an HTTP 400 "not found"."""
    t = tok.strip()
    low = t.lower()
    if low.startswith(ATLAS_PREFIX):
        return ATLAS_PREFIX + t[len(ATLAS_PREFIX) :].strip()
    if low.startswith(OPENROUTER_PREFIX):
        return OPENROUTER_PREFIX + t[len(OPENROUTER_PREFIX) :].strip()
    if low.startswith(LMS_PREFIX):
        return LMS_PREFIX + t[len(LMS_PREFIX) :].strip()  # LM Studio keys are case-sensitive
    if low.startswith(ALI_PREFIX):
        return ALI_PREFIX + t[len(ALI_PREFIX) :].strip()  # preserve model-id casing
    return low


def _validate_groups() -> None:
    """Fail at IMPORT if ``GROUPS``/``GROUP_ALIASES`` are malformed.

    Group expansion is a macro over the caller's list, which means a bad group
    definition doesn't raise — it silently changes what gets asked. All three
    ways to get it wrong degrade quietly and differently:

    - an EMPTY group vanishes, so ``resolve(['x'])`` returns an empty panel with
      an empty ``unknown`` — a refusal that cannot say which token was at fault;
    - an UNKNOWN member is reported under its own name, so the caller who typed
      ``twin`` is told ``unknown model: nope``, a token they never typed;
    - a NESTED group is never expanded (``expand_groups`` is deliberately
      single-pass) and lands in ``unknown`` instead.

    Every one of these is a config edit, not a runtime input, so the right place
    to catch them is here — once, loudly — rather than with defensive branches in
    two resolvers. Nesting is forbidden outright instead of adding recursion.
    A real exception rather than ``assert`` so ``python -O`` can't strip it."""
    for name, members in GROUPS.items():
        if not members:
            raise ValueError(f"model group {name!r} is empty")
        if name in KNOWN or name in ALIASES:
            raise ValueError(f"model group {name!r} shadows a model token")
        for m in members:
            key = ALIASES.get(_canon(m), _canon(m))
            if key in GROUPS:
                raise ValueError(f"model group {name!r} nests group {m!r} (groups don't nest)")
            if key not in KNOWN:
                raise ValueError(f"model group {name!r} has unknown member {m!r}")
    for alias, target in GROUP_ALIASES.items():
        if target not in GROUPS:
            raise ValueError(f"group alias {alias!r} points at missing group {target!r}")


_validate_groups()


def resolve_ordered(models: list | None) -> tuple[list[str], list[str]]:
    """Like ``resolve`` but PRESERVES order and duplicates — for the sequential
    chain, where the order IS the computation (``m3 > glm > fable`` differs from
    ``glm > m3 > fable``) and a repeat like ``fable > glm > fable`` (draft, critique,
    re-decide with the same model) is legitimate. Applies ``GROUPS`` (``twin`` →
    two stages, ``fable`` then ``opus``) and then ``ALIASES`` (e.g. ``m3`` →
    ``minimax``). Returns (recognized_in_order, unknown_in_order)."""
    if not models:
        return [], []
    models = expand_groups(models)
    recognized: list[str] = []
    unknown: list[str] = []
    for m in models:
        canon = _canon(str(m))
        tok = ALIASES.get(canon, canon)
        if not tok:
            continue
        if (
            tok in KNOWN
            or ollama_model(tok)
            or lmstudio_model(tok)
            or atlas_model(tok)
            or openrouter_model(tok)
            or ali_model(tok)
        ):
            recognized.append(tok)
        else:
            unknown.append(tok)
    return recognized, unknown


def resolve(models: list | None) -> tuple[list[str], list[str]]:
    """Split a requested model list into (recognized, unknown), de-duped.

    Named oracles (KNOWN) come first in canonical order; dynamic
    ``ollama:<model>`` / ``atlas:<model>`` / ``openrouter:<model>`` tokens follow
    in requested order (each carries its own model, so they can't live in a fixed
    enum). ``None``/empty falls back to ``default_models()``; an explicit list that
    names nothing recognized does NOT — it resolves to an empty panel, so the caller
    can refuse it instead of silently asking a panel nobody named. A bare
    ``ollama:`` with no model is unknown. Group tokens (``twin`` → fable + opus)
    and operator aliases (``m3`` → ``minimax``, ``gpt`` → ``codex``, ``xai`` →
    ``grok``) are applied exactly as in ``resolve_ordered`` — a council that
    accepts ``m3`` in a chain pipeline must not silently fall back to defaults in
    a fan-out."""
    if not models:
        return default_models(), []
    requested = [ALIASES.get(t, t) for t in (_canon(str(m)) for m in expand_groups(models)) if t]
    known = [k for k in KNOWN if k in requested]
    ollama_tokens: list[str] = []
    lms_tokens: list[str] = []
    atlas_tokens: list[str] = []
    openrouter_tokens: list[str] = []
    ali_tokens: list[str] = []
    gateway_seen: set[str] = set()  # dedupe case-insensitively, keep first-seen casing
    unknown: list[str] = []
    for m in requested:
        if m in KNOWN:
            continue
        if ollama_model(m) and m not in ollama_tokens:
            ollama_tokens.append(m)
        elif lmstudio_model(m) and m.lower() not in gateway_seen:
            gateway_seen.add(m.lower())
            lms_tokens.append(m)
        elif atlas_model(m) and m.lower() not in gateway_seen:
            gateway_seen.add(m.lower())
            atlas_tokens.append(m)
        elif openrouter_model(m) and m.lower() not in gateway_seen:
            gateway_seen.add(m.lower())
            openrouter_tokens.append(m)
        elif ali_model(m) and m.lower() not in gateway_seen:
            gateway_seen.add(m.lower())
            ali_tokens.append(m)
        elif (
            not ollama_model(m)
            and not lmstudio_model(m)
            and not atlas_model(m)
            and not openrouter_model(m)
            and not ali_model(m)
            and m not in unknown
        ):
            unknown.append(m)
    recognized = (
        known + ollama_tokens + lms_tokens + atlas_tokens + openrouter_tokens + ali_tokens
    )
    return recognized, unknown
