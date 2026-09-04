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
import shutil
import sys
import time

from . import (
    anthropic_http,
    atlas,
    cache,
    codex,
    console,
    fable,
    fable51,
    gemini,
    grok,
    health,
    kimi,
    minimax,
    ollama,
    openrouter,
    opus,
    trace_runtime,
)
from .oracle_common import OracleResult  # shared type — lives here to avoid circular imports
from .prompts import SYNTH_SYSTEM_PROMPT
from .provider_telemetry import ProviderTelemetry

KNOWN = (
    "fable", "fable51", "opus", "deepseek", "minimax", "glm", "gemini", "codex", "grok", "kimi",
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
_ANTHROPIC_BRIDGES = {"fable": fable, "fable51": fable51, "opus": opus}
_ANTHROPIC = tuple(_ANTHROPIC_BRIDGES)

# `fable51` is the same tier and price as `fable`, and today resolves to the same
# model — it earns a seat when NAMED, not in a blanket fan-out, so the tier
# presets skip it. Everything else in KNOWN is a distinct voice worth its slot.
_TIER_EXCLUDED = ("fable51",)
_HTTP = ("glm", "deepseek")  # reached via anthropic_http
# Direct-API oracles that fall back to an Atlas-hosted equivalent when their own
# API key is absent. The direct endpoint is cheaper and stays preferred; this only
# keeps the oracle alive on one Atlas key instead of reporting it unavailable.
_ATLAS_FALLBACK = {"glm": "zai-org/glm-5.3"}
OLLAMA_PREFIX = "ollama:"  # dynamic tokens: ollama:<model>, model rides in the key
ATLAS_PREFIX = "atlas:"  # dynamic tokens: atlas:<model> (OpenAI-compatible Atlas Cloud chat)
OPENROUTER_PREFIX = "openrouter:"  # dynamic tokens: openrouter:<model> (~400 models, one key)


def ollama_model(key: str) -> str | None:
    """The Ollama model carried by an ``ollama:<model>`` token, or None."""
    if key.startswith(OLLAMA_PREFIX):
        return key[len(OLLAMA_PREFIX):].strip() or None
    return None


def atlas_model(key: str) -> str | None:
    """The Atlas Cloud model carried by an ``atlas:<model>`` token, or None."""
    if key.startswith(ATLAS_PREFIX):
        return key[len(ATLAS_PREFIX):].strip() or None
    return None


def openrouter_model(key: str) -> str | None:
    """The OpenRouter model carried by an ``openrouter:<model>`` token, or None."""
    if key.startswith(OPENROUTER_PREFIX):
        return key[len(OPENROUTER_PREFIX):].strip() or None
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
        return opus.OPUS_MODEL
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
    am = atlas_model(key)
    if am:
        return am  # display the bare model, e.g. xai/grok-4.6
    om = openrouter_model(key)
    if om:
        return om  # display the bare model, e.g. anthropic/claude-fable-5.1
    return key


def default_models() -> list[str]:
    """DEFAULT plus deepseek when its API key is configured (cheap-first preference).

    Checked at call time so setting/unsetting ASK_FABLE_DEEPSEEK_API_KEY takes
    effect without a restart."""
    return ["fable", "deepseek", "minimax"] if available("deepseek") else list(DEFAULT)


def available(key: str) -> bool:
    """True when this oracle can actually be reached right now."""
    if key in _ANTHROPIC:
        return True  # OAuth session assumed (same as the `ask` / `ask_opus5` tools)
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
    if atlas_model(key):
        return atlas.configured()  # needs an API key for the chat endpoint
    if openrouter_model(key):
        return openrouter.configured()  # ditto — the catalog is free, chat is not
    return False


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
            True if key == "codex"
            else False if key in ("grok", "kimi")
            else None if key == "gemini"
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
        result.telemetry, result.status, result.thinking, kind=result.kind
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
    folded into the message, atop the backend's own scope prompt."""
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
    (uncached) outcome is recorded so the breaker tracks actual backend health."""
    ck = cache.key("oracle", [model_name], question, context, effort=effort)
    cached = cache.get(ck)
    if cached is not None:
        payload, age_s = cached
        trace_runtime.record_stage(
            "cache.inner", "hit", kind=trace_runtime.EventKind.CACHE,
            cache={
                "status": "hit", "layer": "oracle", "age_ms": age_s * 1000,
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
        "cache.inner", "miss", kind=trace_runtime.EventKind.CACHE,
        cache={"status": "miss", "layer": "oracle"},
    )

    # Circuit breaker: skip chronically-failing oracles (cache misses only).
    if health.breaker.should_skip(key):
        skipped = OracleResult("error", key=key, kind="circuit_open",
                               text=f"{key} circuit breaker is open (recent error rate exceeded threshold)",
                               model=model_name)
        # No backend was reached, and only this line knows that — labelling it
        # like a real call would leave the error kind as the sole way to tell a
        # shed call from one that actually ran.
        skipped.telemetry = fallback_telemetry(
            key=key, requested_model=model_name, wall_duration_ms=0.0,
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
    # for that (question, context) for an hour, so re-ask instead.
    if result.status == "ok":
        payload = {
            "status": result.status,
            "text": result.text,
            "kind": result.kind,
            "model": result.model,
            "thinking": result.thinking,
        }
        cache.put(ck, trace_runtime.prepare_cache_store(payload))
    return result


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
            if fallback and atlas.configured():
                r = await atlas.run(fallback, question, context, effort=effort)
                r.key = key  # attribution keeps the oracle key, not the atlas token
                return r
            return OracleResult("error", key=key, text=f"{key} not configured (set ASK_FABLE_{key.upper()}_API_KEY)",
                                kind="not_configured", model=label(key))
        r = await anthropic_http.run(cfg, question, context)
        r.key = key
        return r
    model = ollama_model(key)
    if model:
        if not ollama.configured():
            return OracleResult("error", key=key, text="ollama not reachable (default is a local "
                                "`ollama serve` + `ollama signin`; or set ASK_FABLE_OLLAMA_API_KEY "
                                "for ollama.com)", kind="not_configured", model=model)
        r = await ollama.run(model, question, context)
        r.key = key
        return r
    amodel = atlas_model(key)
    if amodel:
        # Prefer the local `grok` CLI for xAI Grok models when it's installed —
        # same model family, operator's grok.com login, no Atlas API key.
        if grok.looks_like_grok_model(amodel) and grok.available():
            r = await grok.run(question, context, model=amodel, effort=effort)
            r.key = key  # keep the atlas: token so sources stay attributable
            return r
        # Same deal for Moonshot Kimi: the local CLI runs on the operator's Kimi
        # Code subscription instead of per-token Atlas billing. Requires a KNOWN
        # alias — an unmappable Kimi id stays on Atlas rather than silently
        # answering as the default local model under the requested id's name.
        if kimi.available() and kimi.local_alias_for(amodel) is not None:
            r = await kimi.run(question, context, model=amodel, effort=effort)
            r.key = key
            return r
        if not atlas.configured():
            return OracleResult("error", key=key, text="atlas not configured (set "
                                "ASK_FABLE_ATLAS_API_KEY, or ATLASCLOUD_API_KEY which the Atlas "
                                "Cloud MCP server already uses)", kind="not_configured", model=amodel)
        r = await atlas.run(amodel, question, context, effort=effort)
        r.key = key
        return r
    omodel = openrouter_model(key)
    if omodel:
        # Same prefer-local rule as Atlas: a Grok or Kimi id the operator can
        # already serve from an authenticated CLI should not be billed per token
        # by a gateway. Attribution keeps the openrouter: token either way.
        if grok.looks_like_grok_model(omodel) and grok.available():
            r = await grok.run(question, context, model=omodel, effort=effort)
            r.key = key
            return r
        if kimi.available() and kimi.local_alias_for(omodel) is not None:
            r = await kimi.run(question, context, model=omodel, effort=effort)
            r.key = key
            return r
        if not openrouter.configured():
            return OracleResult("error", key=key, text="openrouter not configured (set "
                                "ASK_FABLE_OPENROUTER_API_KEY, or OPENROUTER_API_KEY which "
                                "other OpenRouter tooling already uses)",
                                kind="not_configured", model=omodel)
        r = await openrouter.run(omodel, question, context, effort=effort)
        r.key = key
        return r
    return OracleResult("error", key=key, text=f"unknown oracle: {key}", kind="unknown_oracle", model=key)


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
    "opus5": "opus",
    "opus-5": "opus",
    "claude-opus-5": "opus",
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
        return ATLAS_PREFIX + t[len(ATLAS_PREFIX):].strip()
    if low.startswith(OPENROUTER_PREFIX):
        return OPENROUTER_PREFIX + t[len(OPENROUTER_PREFIX):].strip()
    return low


def _validate_groups() -> None:
    """Fail at IMPORT if ``GROUPS``/``GROUP_ALIASES`` are malformed.

    Group expansion is a macro over the caller's list, which means a bad group
    definition doesn't raise — it silently changes what gets asked. All three
    ways to get it wrong degrade quietly and differently:

    - an EMPTY group vanishes, so ``resolve(['x'])`` falls through to
      ``recognized or default_models()`` and returns the DEFAULT council with an
      empty ``unknown`` — indistinguishable from asking for nothing at all;
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
        if tok in KNOWN or ollama_model(tok) or atlas_model(tok) or openrouter_model(tok):
            recognized.append(tok)
        else:
            unknown.append(tok)
    return recognized, unknown


def resolve(models: list | None) -> tuple[list[str], list[str]]:
    """Split a requested model list into (recognized, unknown), de-duped.

    Named oracles (KNOWN) come first in canonical order; dynamic
    ``ollama:<model>`` / ``atlas:<model>`` / ``openrouter:<model>`` tokens follow
    in requested order (each carries its own model, so they can't live in a fixed
    enum). ``None``/empty — or an all-unknown list — falls back to
    ``default_models()``. A bare ``ollama:`` with no model is unknown. Group tokens
    (``twin`` → fable + opus) and operator aliases (``m3`` → ``minimax``, ``gpt`` →
    ``codex``, ``xai`` → ``grok``) are applied exactly as in ``resolve_ordered`` — a
    council that accepts ``m3`` in a chain pipeline must not silently fall back to
    defaults in a fan-out."""
    if not models:
        return default_models(), []
    requested = [ALIASES.get(t, t) for t in (_canon(str(m)) for m in expand_groups(models)) if t]
    known = [k for k in KNOWN if k in requested]
    ollama_tokens: list[str] = []
    atlas_tokens: list[str] = []
    openrouter_tokens: list[str] = []
    gateway_seen: set[str] = set()  # dedupe case-insensitively, keep first-seen casing
    unknown: list[str] = []
    for m in requested:
        if m in KNOWN:
            continue
        if ollama_model(m) and m not in ollama_tokens:
            ollama_tokens.append(m)
        elif atlas_model(m) and m.lower() not in gateway_seen:
            gateway_seen.add(m.lower())
            atlas_tokens.append(m)
        elif openrouter_model(m) and m.lower() not in gateway_seen:
            gateway_seen.add(m.lower())
            openrouter_tokens.append(m)
        elif not ollama_model(m) and not atlas_model(m) and not openrouter_model(m):
            unknown.append(m)
    recognized = known + ollama_tokens + atlas_tokens + openrouter_tokens
    return (recognized or default_models()), unknown
