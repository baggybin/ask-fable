"""The ask_fable MCP server: guard a question, route to oracles, return JSON.

Modeled on graphify's low-level ``mcp.server.Server`` usage. Exposes the full
tool surface (``ask``, single-oracle tools, council/chain/debate, context bus,
traces, hub session dashboard, …). Every response is a single ``TextContent``
carrying a JSON payload so callers across harnesses get a stable, parseable
result.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import sys
import time
import weakref
from contextvars import ContextVar
from pathlib import Path

import mcp.types as types
from mcp.server import Server
from mcp.shared.exceptions import McpError

from . import (
    ali,
    atlas,
    audit,
    cache,
    codeindex,
    codex,
    conference,
    config,
    context_bus,
    context_store,
    controlpage,
    debate_ledger,
    diagnose,
    fable,
    falsify,
    gemini,
    grok,
    guard,
    hub,
    kimi,
    lmstudio,
    minimax,
    ollama,
    openrouter,
    opus,
    oracles,
    outputs,
    resolver,
    safe_fs,
    sidecar,
    stats,
    trace_bundle,
    trace_query,
    trace_runtime,
    verify,
    websearch,
)
from .console import Reporter
from .prompts import (
    ASK_CHAIN_TOOL_DESCRIPTION,
    ASK_CONFERENCE_TOOL_DESCRIPTION,
    ASK_COUNCIL_TOOL_DESCRIPTION,
    ASK_DEBATE_TOOL_DESCRIPTION,
    ASK_FABLE_HELP_TOOL_DESCRIPTION,
    ASK_FALSIFY_TOOL_DESCRIPTION,
    ASK_MODEL_TOOL_DESCRIPTION,
    ASK_TOOL_DESCRIPTION,
    ASK_VERIFY_TOOL_DESCRIPTION,
    ASK_WEBSEARCH_TOOL_DESCRIPTION,
    CODE_INDEX_TOOL_DESCRIPTION,
    CODE_SEARCH_TOOL_DESCRIPTION,
    CONFIGURE_COUNCIL_TOOL_DESCRIPTION,
    CONFIGURE_TRACING_TOOL_DESCRIPTION,
    CONTEXT_READ_TOOL_DESCRIPTION,
    CONTEXT_TOOL_DESCRIPTION,
    DIAGNOSE_TOOL_DESCRIPTION,
    HOST_STATUS_TOOL_DESCRIPTION,
    LIST_MODELS_TOOL_DESCRIPTION,
    SERVER_INSTRUCTIONS,
    SESSION_LIST_TOOL_DESCRIPTION,
    SESSION_PEEK_TOOL_DESCRIPTION,
    SESSION_STATS_TOOL_DESCRIPTION,
    STATS_TOOL_DESCRIPTION,
    SYNTH_SYSTEM_PROMPT,
    UNLOAD_LMS_MODEL_TOOL_DESCRIPTION,
    compose_chain_step,
    compose_debate_step,
    compose_synth,
    help_text,
)
from .sessions import SessionStore


def _preview(text: str, n: int = 60) -> str:
    text = " ".join((text or "").split())
    return f"{text[:n]}… ({len(text)} chars)" if len(text) > n else f"{text} ({len(text)} chars)"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _flag(name: str) -> bool:
    # config → env → default, so boolean flags are runtime-togglable (config overrides env).
    return (config.setting(name) or "").lower() in ("1", "true", "yes", "on")


def _max_parallel() -> int:
    """Max simultaneous oracle calls in a council fan-out (``ASK_FABLE_MAX_PARALLEL``,
    default 6). Bounds socket use on the ``full`` tier so a 12-model fan-out can't
    exhaust ``ulimit -n`` or saturate provider rate limits."""
    return max(1, _int_env("ASK_FABLE_MAX_PARALLEL", 6))


def _council_timeout_s() -> float:
    """Upper bound on an ``ask_council`` PANEL (``ASK_FABLE_COUNCIL_TIMEOUT``,
    default ``ASK_FABLE_TIMEOUT + 120``). Bounds the worst case where a backend
    swallows its own inner timeout. Not a whole-call cap: synthesis (and its Fable
    retry) runs after the panel under the synthesizer's own timeout, and the
    sequential LM Studio council applies this to each member in turn."""
    return float(_int_env("ASK_FABLE_COUNCIL_TIMEOUT", _int_env("ASK_FABLE_TIMEOUT", 240) + 120))


def _chain_timeout_s(n: int) -> float:
    """Hard upper bound on the ``ask_chain`` stage pipeline (``ASK_FABLE_CHAIN_TIMEOUT``,
    default ``max(600, n * ASK_FABLE_TIMEOUT)``). The chain is sequential so the
    default scales with the number of stages. The Fable fallback synthesis that runs
    when the final stage fails comes after the pipeline, under Fable's own timeout."""
    per = _int_env("ASK_FABLE_TIMEOUT", 240)
    raw = _int_env("ASK_FABLE_CHAIN_TIMEOUT", max(600, n * per))
    return float(max(10, raw))


def _add_thinking(payload: dict, text: str) -> None:
    """Attach a capped reasoning excerpt to a result payload — opt-in via
    ASK_FABLE_RETURN_THINKING, kept OUT by default so the tool result the agent reads
    stays lean (the full trace always goes to the on-disk dump). ASK_FABLE_THINKING_CHARS
    tunes the cap (default 4000)."""
    if not _flag("ASK_FABLE_RETURN_THINKING"):
        return
    t = (text or "").strip()
    if not t:
        return
    cap = _int_env("ASK_FABLE_THINKING_CHARS", 4000)
    if cap <= 0:  # 0 disables the excerpt entirely rather than emitting a bare " …"
        return
    payload["thinking"] = (t[:cap].rstrip() + " …") if len(t) > cap else t


# ── Schema builders ─────────────────────────────────────────────────────
# Single-oracle (and multi-tool) schemas share question + context + context_ref.
# Build them once so wording / anyOf shape can't drift across 10+ tools.

_CONTEXT_PROP = {
    "type": "string",
    "default": "",
    "description": "Optional code snippets, file paths, or structural context.",
}

_CONTEXT_REF_PROP = {
    "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
    "description": (
        'Key(s) of context saved with `context(op="write", …)` to pull in and prepend '
        "to `context` — paste a big context ONCE, reference it by key here. "
        "Missing keys are reported, not fatal."
    ),
}

# Hub coordination key for multi-oracle tools (not multi-turn Fable resume).
_HUB_SESSION_PROP = {
    "type": "string",
    "description": (
        "Optional coordination key for the cross-agent hub (`session_list` / "
        "`session_peek`). Reuse the same key across agents working the same "
        "decision so turns group together. Defaults to the tool name "
        "(`ask_council` / `ask_chain` / `ask_debate` / …) when omitted."
    ),
}

# Operator-authorized denylist override. On EVERY tool (via `_q_ctx_props`), since
# the guard covers the question and — by default — `context`, and legitimate
# security-engineering work needs one escape hatch. It authorizes nothing on its
# own: `_trusted_allowed()` gates it on ASK_FABLE_ALLOW_TRUSTED, because tool
# arguments come from the very agent the guard constrains.
_TRUSTED_PROP = {
    "type": "boolean",
    "default": False,
    "description": "Operator-authorized. When true, the prohibited-use denylist "
    "runs in log-only mode: security vocabulary in the question AND in `context` is "
    "audited but does not block. Use for legitimate security-engineering work (PoC "
    "analysis, CVE research, binary hardening review) where the ask genuinely needs "
    "security terms. Takes effect ONLY when the operator has set "
    "ASK_FABLE_ALLOW_TRUSTED (env or config); otherwise the flag is ignored "
    "and the denylist still applies.",
}


def _q_ctx_props(
    question_desc: str,
    *,
    context_desc: str | None = None,
    context_ref_desc: str | None = None,
    **extra: object,
) -> dict:
    """question + context + context_ref + trusted, plus any extra properties."""
    ctx = dict(_CONTEXT_PROP)
    if context_desc is not None:
        ctx["description"] = context_desc
    cref = dict(_CONTEXT_REF_PROP)
    if context_ref_desc is not None:
        cref["description"] = context_ref_desc
    return {
        "question": {"type": "string", "description": question_desc},
        "context": ctx,
        "context_ref": cref,
        "trusted": _TRUSTED_PROP,
        **extra,
    }


def _tool_schema(
    properties: dict,
    *,
    required: list[str] | None = None,
) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": ["question"] if required is None else required,
        "additionalProperties": False,
    }


# `ask` is the STATEFUL (multi-turn) tool; `oracle` picks which multi-turn model.
# The whole Opus family names the ONE Opus session — mirroring `_handle_reset`'s
# `canon.startswith("opus")` rule — so every spelling below selects it.
_ASK_ORACLE_SPELLINGS = ("fable", "opus", "opus5", "opus55", "opus48") + tuple(
    sorted(a for a, t in oracles.ALIASES.items() if t in ("opus5", "opus55", "opus48"))
)

_ASK_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific question about concrete software code/architecture "
        "(structure, functionality, data flow, module/function relationships, routing).",
        context_ref_desc=(
            'Key(s) of context previously saved with `context(op="write", …)` to pull in '
            "and prepend to `context` — so you paste a big codebase context ONCE and reference "
            "it by key across many asks instead of re-pasting. Missing keys are reported, not fatal."
        ),
        oracle={
            "type": "string",
            "default": "fable",
            "enum": list(_ASK_ORACLE_SPELLINGS),
            "description": "Which multi-turn model answers: 'fable' (default) or the Opus "
            "family — 'opus' tracks the newest Claude Opus, and 'opus5'/'opus55'/'opus48' "
            "name the same Opus session. Opus is roughly half Fable's price and faster, so "
            "prefer it for high-volume or long back-and-forth work. For a single-turn model "
            "use `ask_model(provider=…)`.",
        },
        session={
            "type": "string",
            "default": "default",
            "description": "Conversation key. Reuse it to ask follow-ups (the model keeps "
            "context); use a new key or reset=true to start a fresh topic. Fable and Opus "
            "sessions are namespaced separately, so the same key on each is two independent "
            "conversations.",
        },
        reset={
            "type": "boolean",
            "default": False,
            "description": "Dump+clear this session before asking, starting a fresh conversation.",
        },
    )
)

# `ask_opus5` is the Opus-5 twin of `ask` — same multi-turn contract, same
# arguments — so it reuses the ask grammar with Opus-flavored session wording.
_OPUS_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific question about concrete software code/architecture "
        "(structure, functionality, data flow, module/function relationships, routing).",
        context_ref_desc=_ASK_SCHEMA["properties"]["context_ref"]["description"],
        session={
            "type": "string",
            "default": "default",
            "description": "Conversation key. Reuse it to ask follow-ups (Opus 5 keeps "
            "context); use a new key or reset=true to start a fresh topic. Opus sessions "
            "are namespaced separately from `ask`'s Fable sessions, so the same key on "
            "both tools is two independent conversations.",
        },
        reset=_ASK_SCHEMA["properties"]["reset"],
        trusted=_ASK_SCHEMA["properties"]["trusted"],
    )
)

_SONNET_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask Claude Sonnet 5 on its own "
        "— a cheap, fast Anthropic model on the same OAuth session as `ask`."
    )
)
_M3_SCHEMA = _tool_schema(
    _q_ctx_props("A specific software/engineering question to ask MiniMax (MiniMax-M3) on its own.")
)
_GLM_SCHEMA = _tool_schema(
    _q_ctx_props("A specific software/engineering question to ask GLM (GLM-5.2) on its own.")
)
_DEEPSEEK_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask DeepSeek (deepseek-v4-pro) on its own."
    )
)
_GEMINI_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask Gemini (Gemini 3.1 Pro) on its own."
    )
)
_CODEX_SCHEMA = _tool_schema(
    _q_ctx_props("A specific software/engineering question to ask Codex (GPT-5.6 Sol) on its own.")
)
_GROK_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask Grok (grok-4.6) on its own "
        "via the local `grok` CLI."
    )
)
_KIMI_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask Kimi (kimi-code/k3) on its own "
        "via the local `kimi` CLI."
    )
)

# The consolidated single-model tool: one schema for every stateless oracle.
# `provider` selects the backend; `model` overrides it where the backend accepts
# one. This replaces the per-backend ask_* tools (ask_m3, ask_glm, ask_sonnet, …) —
# they stay callable as unadvertised aliases, but only `ask_model` is advertised.
_MODEL_PROVIDERS = (
    "sonnet",
    "minimax",
    "glm",
    "deepseek",
    "gemini",
    "codex",
    "grok",
    "kimi",
    "ollama",
    "lmstudio",
    "atlas",
    "ali",
    "openrouter",
)
# Spellings the schema must accept: canonical keys plus every alias that resolves
# into this set (m3→minimax, gpt→codex, xai→grok, claude-sonnet-5→sonnet, …), so a
# strictly-validating MCP host doesn't reject a documented token client-side.
_MODEL_PROVIDER_SPELLINGS = _MODEL_PROVIDERS + tuple(
    sorted(a for a, t in oracles.ALIASES.items() if t in _MODEL_PROVIDERS)
)

_MODEL_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask ONE model on its own — "
        "guarded, single-turn. `provider` selects the backend; `model` overrides "
        "the model where the backend accepts one.",
        context_ref_desc=(
            'Key(s) of context saved with `context(op="write", …)` to pull in and prepend '
            "to `context`. Missing keys are reported, not fatal."
        ),
        provider={
            "type": "string",
            "enum": list(_MODEL_PROVIDER_SPELLINGS),
            "description": (
                "Which backend answers. Cheap direct APIs (fixed model, no `model`): "
                "'minimax' (MiniMax-M3), 'deepseek', 'glm'. Local CLIs: 'gemini', "
                "'codex' (GPT-5.6 Sol), 'grok', 'kimi'. OAuth: 'sonnet'. "
                "Gateways / CLI overrides (pass `model`): 'ollama', 'lmstudio', "
                "'atlas', 'ali' (Alibaba/Qwen), 'openrouter'. Aliases: m3=minimax, "
                "gpt=codex, xai=grok."
            ),
        },
        model={
            "type": "string",
            "description": (
                "Model id for a provider that accepts one: a CLI override (grok, "
                "kimi) or a gateway model (ollama, lmstudio, atlas, ali, "
                "openrouter). Rejected for the fixed-model providers (sonnet, "
                "minimax, glm, deepseek, gemini, codex). Omit to use the server "
                "default; call `list_models(provider=…)` for a gateway catalogue."
            ),
        },
        effort={
            "type": "string",
            "enum": ["quick", "standard", "deep"],
            "description": (
                "Answer budget / reasoning depth, honored by atlas, openrouter, "
                "grok and kimi; ignored by the other providers (atlas/openrouter "
                "default to 'deep')."
            ),
        },
    ),
    required=["provider", "question"],
)

_WEBSEARCH_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A web-search / OSINT research task — a question to research on the LIVE web "
        "(current facts, versions/pricing/releases, who/what-is, open-source intel).",
        context_desc=(
            "Optional code, artifact, URL, or entity the research is ABOUT "
            "(e.g. a library, error text, CVE id, or organization)."
        ),
        model={
            "type": "string",
            "enum": ["grok", "gemini", "sonnet", "opus48", "opus5", "fable"],
            "description": (
                "Which search-capable model runs the research. `grok` (grok-4.6 live "
                "search) is the default; `sonnet` / `opus48` / `opus5` / `fable` use "
                "Claude native WebSearch over the OAuth session; `gemini` uses the "
                "local `agy` CLI and is SEARCH-ONLY — agy's headless policy denies "
                "page fetch/shell/file tools and a denial aborts the turn, so that "
                "backend is told to use search_web only. Omit to use the "
                "ASK_FABLE_WEBSEARCH_MODEL default (grok)."
            ),
        },
    )
)

_COUNCIL_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask the selected models; "
        "Fable then synthesizes their answers into one.",
        context_desc=(
            "Optional code snippets, file paths, or structural context (shared by all models)."
        ),
        session=_HUB_SESSION_PROP,
        provider={
            "type": "string",
            "enum": ["ollama", "atlas", "openrouter", "lmstudio"],
            "description": "Scope the council to ONE gateway: the default panel comes "
            "from that provider's configured set (for atlas/openrouter, else a live-"
            "catalog shortlist), members are its tokens, and the adjudicator follows "
            "that provider's ladder (GPT-first for atlas/openrouter; Fable otherwise). "
            "`lmstudio` runs the panel ONE AT A TIME (a single GPU serves one model at "
            "a time). An explicit `models` is honored within the chosen provider; "
            "`tier` is ignored when `provider` is set. Omit for a mixed council "
            "selected by `models`/`tier`.",
        },
        models={
            "type": "array",
            "items": {
                # Must accept everything oracles.resolve() accepts — a schema
                # narrower than the handler makes strictly-validating MCP hosts
                # reject documented calls (atlas:/alias tokens) client-side.
                "anyOf": [
                    {
                        "type": "string",
                        "enum": list(oracles.KNOWN)
                        + sorted(oracles.ALIASES)
                        + sorted(oracles.GROUPS)
                        + sorted(oracles.GROUP_ALIASES),
                    },
                    {"type": "string", "pattern": "^ollama:.+"},
                    {"type": "string", "pattern": "^atlas:.+"},
                    {"type": "string", "pattern": "^openrouter:.+"},
                    {"type": "string", "pattern": "^lmstudio:.+"},
                    {"type": "string", "pattern": "^ali:.+"},
                    # A bare 'vendor/model' id — allowed so a provider-scoped
                    # council (`provider=atlas|openrouter`) can pass the ids its
                    # `list_models` returned without hand-prefixing them.
                    {"type": "string", "pattern": "^[^\\s:]+/[^\\s:]+$"},
                ]
            },
            "description": "Explicit list of models to ask, from "
            "['fable','opus','deepseek','minimax','glm','gemini','codex','grok','kimi'] "
            "(aliases: 'm3' = minimax, 'gpt' = codex, 'xai' = grok, 'opus5' = opus), "
            "plus any 'ollama:<model>' cloud token (e.g. 'ollama:kimi-k2.7-code:cloud') "
            "or 'atlas:<model-id>' token (e.g. 'atlas:zai-org/glm-5.2'). "
            "One entry may be the group token 'twin' (aka 'twin flames'), which expands "
            "to BOTH Anthropic reasoners — fable + opus — on the one OAuth session, so "
            "['twin'] is a dual Fable/Opus 5 invocation and ['twin','minimax'] adds a "
            "third voice to it. "
            "Overrides `tier` when given. 'glm'/'deepseek', 'ollama:*' and 'atlas:*' require "
            "API keys configured on the server; unconfigured ones are reported and skipped, "
            "not fatal.",
        },
        tier={
            "type": "string",
            "enum": list(oracles.TIERS),
            "default": "default",
            "description": "Named council preset (used when `models` is omitted): "
            "'default' = fable+minimax, +deepseek when its API key is configured; "
            "'twin' = the twin flames, fable+opus — a dual Fable/Opus 5 invocation "
            "needing no provider keys at all; "
            "'middle' = +opus+glm+gemini+codex+grok+kimi (cheap models first); "
            "'full' = +the configured Ollama Cloud models (ASK_FABLE_OLLAMA_COUNCIL).",
        },
        synthesizer={
            "anyOf": [
                {"type": "string", "enum": list(oracles.KNOWN) + sorted(oracles.ALIASES)},
                {"type": "string", "pattern": "^ollama:.+"},
                {"type": "string", "pattern": "^atlas:.+"},
                {"type": "string", "pattern": "^openrouter:.+"},
                {"type": "string", "pattern": "^ali:.+"},
            ],
            "description": "Model that reconciles the panel answers into one (default "
            "'fable'). Any council token works: 'opus' (Claude Opus 5 — cheaper and "
            "faster than Fable), 'codex' (alias 'gpt', GPT-5.6 Sol via the "
            "local CLI), 'atlas:openai/gpt-5.6-sol', 'ollama:<model>', … It may also be a "
            "panel member — its own answer is anonymized and read last. If it is "
            "unavailable or fails, synthesis falls back to Fable (see `synthesis` in the "
            "result).",
        },
    )
)

_CHAIN_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to thread through the pipeline.",
        context_desc=(
            "Optional code snippets, file paths, or structural context (seen by every stage)."
        ),
        context_ref_desc=(
            'Key(s) of context saved with `context(op="write", …)` to pull in and prepend to `context`.'
        ),
        session=_HUB_SESSION_PROP,
        pipeline={
            "type": "string",
            "description": "The ordered pipeline as a string, e.g. 'm3 > glm > deepseek > fable'. "
            "Split on '>'. Order matters and repeats are allowed. Aliases: 'm3' = minimax. "
            "The group token 'twin' (aka 'twin flames') expands in place to two stages, "
            "fable then opus — positionally, so a member you also name elsewhere in the "
            "pipeline runs twice (repeats are legitimate here and are not collapsed). "
            "Ignored when `models` is given.",
        },
        models={
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "string", "pattern": "^ollama:.+"},
                ]
            },
            "description": "The ordered pipeline as an array (alternative to `pipeline`), e.g. "
            "['minimax','glm','fable']. Order-sensitive; duplicates allowed.",
        },
    )
)

_DEBATE_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A contentious, hard-to-reverse software/engineering decision to debate "
        "(e.g. 'is this concurrency design sound?', 'approach X or Y?').",
        context_desc=(
            "Optional code snippets, file paths, or structural context (seen by both sides)."
        ),
        context_ref_desc=(
            'Key(s) of context saved with `context(op="write", …)` to pull in and prepend to `context`.'
        ),
        session=_HUB_SESSION_PROP,
        proposer={
            "type": "string",
            "default": "fable",
            "description": "Model that proposes the position (default 'fable'). "
            "Aliases: 'm3'=minimax, 'gpt'=codex.",
        },
        opponent={
            "type": "string",
            "default": "minimax",
            "description": "Model that refutes it (default 'minimax'). "
            "Try 'codex' (GPT-5.6 Sol) or 'glm'.",
        },
        adjudicator={
            "type": "string",
            "default": "fable",
            "description": "Model that rules on the contested claims (default 'fable'; "
            "'opus' for Claude Opus 5, 'codex' for GPT-5.6 Sol, …). It sees the ledger "
            "anonymized. Any council token works; keep it off the debating pair so the "
            "ruling stays third-party.",
        },
        rounds={
            "type": "integer",
            "minimum": 1,
            "maximum": 2,
            "default": 1,
            "description": "1 (propose→refute→revise, default) or 2 "
            "(adds a rebuttal pass before adjudication).",
        },
    )
)

_VERIFY_SCHEMA = _tool_schema(
    _q_ctx_props(
        "The question the draft answer was written for — what it was supposed to answer.",
        context_desc=(
            "The SOURCE MATERIAL a `cite` receipt may quote — code, specs, docs. This is "
            "deliberately everything EXCEPT the draft: quoting the draft back at itself "
            "proves nothing, so without source material no citation is possible and the "
            "review can only produce opinion."
        ),
        context_ref_desc=(
            'Key(s) of context saved with `context(op="write", …)` to pull in and prepend to `context`.'
        ),
        session=_HUB_SESSION_PROP,
        answer={
            "type": "string",
            "description": "REQUIRED — the draft answer to review, as produced by another "
            "model, another tool, or you. It is returned unchanged; ask_verify never "
            "withholds or rewrites it.",
        },
        reviewer={
            "type": "string",
            "default": "opus",
            "description": "Model that reviews the draft (default 'opus'). Aliases: "
            "'m3'=minimax, 'gpt'=codex.",
        },
        drafted_by={
            "type": "string",
            "description": "Optional — the model that WROTE the draft. When given, a "
            "same-lab review is refused: a family grading its own homework agrees with "
            "itself for reasons unrelated to the draft being right.",
        },
    ),
    required=["question", "answer"],
)

_FALSIFY_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A contentious, CHECKABLE software/engineering claim or question to grind down to "
        "what survives evidence (e.g. 'is this API idempotent as documented?').",
        context_desc=(
            "The corpus the claims must cite — code, specs, docs. `cite` receipts are "
            "checked for VERBATIM presence here."
        ),
        context_ref_desc=(
            'Key(s) of context saved with `context(op="write", …)` to pull in and prepend to `context`.'
        ),
        session={
            "type": "string",
            "description": "REQUIRED — the ledger's persistence key. Reuse it to continue "
            "the same falsification (a killed claim stays dead); a new key starts fresh.",
        },
        assertor={
            "type": "string",
            "default": "minimax",
            "description": "Model that asserts claims (default 'minimax'). "
            "Aliases: 'm3'=minimax, 'gpt'=codex.",
        },
        falsifier={
            "type": "string",
            "default": "opus",
            "description": "Model that attacks the claims (default 'opus'). Must resolve to "
            "a DIFFERENT lab than the assertor — a model must not grade its own family.",
        },
        rounds={
            "type": "integer",
            "minimum": 1,
            "maximum": 6,
            "default": 1,
            "description": "Assert->attack cycles to run this call (default 1). Call again "
            "with the same `session` to advance the persisted ledger further.",
        },
        metamorph={
            "type": "boolean",
            "default": False,
            "description": "Also run a metamorphic stability check on unsupported claims: "
            "restate a claim (semantics-preserving) and re-ask the assertor COLD — a claim that "
            "flips is unstable and cannot compound; a stable one earns WEAK support (the only "
            "way a claim survives with no corpus to cite and no code to run). Costs 2 extra "
            "model calls per unsupported claim; stability is not truth, so stable-but-unverified "
            "survivors are reported separately as `stable_unverified`.",
        },
    ),
    required=["question", "session"],
)

_CONFERENCE_SCHEMA = _tool_schema(
    _q_ctx_props(
        "The topic or open question for the models to brainstorm and argue together.",
        context_desc=(
            "Optional code snippets, file paths, or structural context (shared by all participants)."
        ),
        session=_HUB_SESSION_PROP,
        models={
            "type": "array",
            "items": {
                "anyOf": [
                    {
                        "type": "string",
                        "enum": list(oracles.KNOWN)
                        + sorted(oracles.ALIASES)
                        + sorted(oracles.GROUPS)
                        + sorted(oracles.GROUP_ALIASES),
                    },
                    {"type": "string", "pattern": "^ollama:.+"},
                    {"type": "string", "pattern": "^atlas:.+"},
                    {"type": "string", "pattern": "^openrouter:.+"},
                    {"type": "string", "pattern": "^lmstudio:.+"},
                    {"type": "string", "pattern": "^ali:.+"},
                ]
            },
            "description": "The debater bench (2 or more). From "
            "['fable','opus','deepseek','minimax','glm','gemini','codex','grok','kimi'] plus "
            "any 'ollama:<model>' / 'atlas:<id>' / 'openrouter:<id>' token. Omit to be offered a "
            "native picker (when the client supports elicitation), else it falls back to the "
            "available subset of fable/opus/deepseek/minimax/glm.",
        },
        rounds={
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "default": 3,
            "description": "How many rounds of turns each participant takes (default 3, up to 10).",
        },
        synthesizer={
            "type": "string",
            "default": "fable",
            "description": "Model that writes the closing map of the disagreement "
            "(default 'fable'; 'opus', 'codex', …).",
        },
        attack_premise={
            "type": "boolean",
            "default": True,
            "description": "After the blind round, name the premise every opening assumed "
            "and assign one participant to argue it is FALSE (two extra calls). This is the "
            "challenge nobody else will make, since agreeing on the premise is what lets the "
            "rest of the discussion happen. Needs 3+ seats and 2+ rounds; skipped otherwise.",
        },
        interactive={
            "type": "boolean",
            "default": True,
            "description": "When true and `models` is omitted, pop a native model picker "
            "if the MCP client supports form elicitation. Set false to skip it.",
        },
    ),
    required=[],
)

_OLLAMA_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask a single Ollama Cloud model.",
        model={
            "type": "string",
            "description": "Ollama Cloud model id (e.g. 'kimi-k2.7-code:cloud', "
            "'gpt-oss:120b-cloud'). Omit to use the server's ASK_FABLE_OLLAMA_MODEL default.",
        },
    )
)

_OLLAMA_COUNCIL_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask several Ollama "
        "Cloud models; Fable then synthesizes their answers into one.",
        context_desc=(
            "Optional code snippets, file paths, or structural context (shared by all models)."
        ),
        session=_HUB_SESSION_PROP,
        models={
            "type": "array",
            "items": {"type": "string"},
            "description": "Ollama Cloud model ids (e.g. ['kimi-k2.7-code:cloud', "
            "'gpt-oss:120b-cloud']); an 'ollama:' prefix is optional. Omit to use the "
            "server's configured set (ASK_FABLE_OLLAMA_COUNCIL). Requires "
            "ASK_FABLE_OLLAMA_API_KEY on the server.",
        },
    )
)

_LIST_OLLAMA_SCHEMA = {
    "type": "object",
    "properties": {
        "refresh": {
            "type": "boolean",
            "default": True,
            "description": "Fetch the live ollama.com catalog + locally-pulled models. "
            "When false, only report the currently-configured council (no network).",
        },
    },
    "required": [],
    "additionalProperties": False,
}

_LMS_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask a single model on an LM Studio server.",
        model={
            "type": "string",
            "description": "LM Studio model key (e.g. 'qwen/qwen3.8-27b', "
            "'openai/gpt-oss-120b'). Omit to use the configured default "
            "(ASK_FABLE_LMSTUDIO_MODEL) or the single resident model. Call "
            "`list_lms_models` to see what's on the server.",
        },
    )
)

_LIST_LMS_SCHEMA = {
    "type": "object",
    "properties": {
        "refresh": {
            "type": "boolean",
            "default": True,
            "description": "Fetch the live model catalog from the LM Studio server. "
            "When false, report only the configured defaults (no network).",
        },
    },
    "required": [],
    "additionalProperties": False,
}

_LMS_COUNCIL_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask several LM Studio "
        "models; Fable then synthesizes their answers into one.",
        context_desc=(
            "Optional code snippets, file paths, or structural context (shared by all models)."
        ),
        session=_HUB_SESSION_PROP,
        models={
            "type": "array",
            "items": {"type": "string"},
            "description": "LM Studio model keys (e.g. ['qwen/qwen3.6-35b-a3b', "
            "'google/gemma-4-31b-qat']); an 'lmstudio:' prefix is optional. Omit to use "
            "the configured panel (lmstudio_council / ASK_FABLE_LMSTUDIO_COUNCIL).",
        },
        synthesizer={
            "type": "string",
            "description": "Model that reconciles the panel (default 'fable'; e.g. "
            "'opus', 'codex', or an 'lmstudio:<model>' token to synthesize locally).",
        },
    )
)

_UNLOAD_LMS_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {
            "type": "string",
            "description": "The loaded model key to unload (e.g. "
            "'qwen3.8-27b-distill-q38'). Call `list_lms_models` first to show the "
            "operator what is loaded and how much each occupies.",
        },
    },
    "required": ["model"],
    "additionalProperties": False,
}

_HOST_STATUS_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

_DIAGNOSE_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

_ATLAS_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask an Atlas Cloud text model.",
        model={
            "type": "string",
            "description": "Atlas Cloud model id (e.g. 'xai/grok-4.6', 'openai/gpt-5.6-sol', "
            "'anthropic/claude-opus-4.8'). Omit to use the server's default. Call "
            "`list_atlas_models` to see the live catalog with pricing, then offer the user a "
            "selection menu.",
        },
        effort={
            "type": "string",
            "enum": ["quick", "standard", "deep"],
            "default": "deep",
            "description": "Answer budget / reasoning depth (default 'deep' — max reasoning). "
            "'quick' (~1k tokens, concise), 'standard' (~4k tokens), 'deep' (~16k tokens, "
            "opportunistically sends reasoning_effort:high). Atlas has no documented "
            "reasoning_effort, so effort maps to max_tokens + timeout + a prompt nudge. "
            "Atlas's gateway returns HTTP 504 for any request still running at ~242s, so a "
            "long 'deep' generation can fail outright; retry at 'standard'/'quick', or set "
            "config `atlas_max_tokens` / `atlas_timeout` (env ASK_FABLE_ATLAS_MAX_TOKENS / "
            "ASK_FABLE_ATLAS_TIMEOUT) to fit its cap and wall-clock to the window.",
        },
    )
)

_ALI_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question for an Alibaba Cloud (Qwen) reasoning model.",
        model={
            "type": "string",
            "description": "Alibaba/Qwen MaaS model id (e.g. 'qwen3.8-max', 'qwen3.8-flash', "
            "'qwen3.7-plus'). The gateway also fronts deepseek-*/glm-* and an 'auto' router. "
            "Omit to use the server's default (qwen3.8-max). Call `list_ali_models` for the live "
            "reasoning-model catalog. Reasoning ('thinking') is captured automatically.",
        },
    )
)

_OPENROUTER_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask an OpenRouter model.",
        model={
            "type": "string",
            "description": "OpenRouter model id (e.g. 'anthropic/claude-fable-5.1', "
            "'openai/gpt-5.6-sol', 'deepseek/deepseek-v4.1-flash', 'google/gemini-3.8-flash'). "
            "Omit to use the server's default. Call `list_openrouter_models` to see the live "
            "catalog with pricing and per-model reasoning support, then offer the user a "
            "selection menu.",
        },
        effort={
            "type": "string",
            "enum": ["quick", "standard", "deep"],
            "default": "deep",
            "description": "Answer budget / reasoning depth (default 'deep' — max reasoning). "
            "'quick' (~1k tokens, concise), 'standard' (~4k tokens), 'deep' (~16k tokens). "
            "Unlike Atlas, OpenRouter publishes each model's supported reasoning efforts, so "
            "'deep' sends the highest effort the CHOSEN model actually accepts and omits the "
            "field entirely for non-reasoning models — no wasted probe request. Also unlike "
            "Atlas, no fixed ~242s gateway cutoff was measured (a 16k-token non-streaming call "
            "returned in 314s), so long calls are bounded by the client timeout, not the "
            "gateway.",
        },
    )
)

_LIST_OPENROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "refresh": {
            "type": "boolean",
            "default": True,
            "description": "Fetch the live OpenRouter catalog (no auth needed). When false, "
            "only report the effort choices (no network).",
        },
        "task": {
            "type": "string",
            "minLength": 3,
            "description": "Optional job to rank the live catalog for, such as 'debug a large "
            "Rust repository' or 'cheap high-volume summarizing'. Ranking uses the catalog's "
            "own data (reasoning support, context length, price, release date) rather than a "
            "hand-maintained list of model families.",
        },
        "limit": {
            "type": "integer",
            "minimum": 2,
            "maximum": 8,
            "default": 5,
            "description": "Maximum task-matched models to offer.",
        },
        "interactive": {
            "type": "boolean",
            "default": True,
            "description": "When a task is supplied, open a native model + effort picker if "
            "the MCP client supports form elicitation; otherwise return picker JSON.",
        },
    },
    "required": [],
    "additionalProperties": False,
}

_LIST_ALI_SCHEMA = {
    "type": "object",
    "properties": {
        "refresh": {
            "type": "boolean",
            "default": True,
            "description": "Fetch the live Alibaba/Qwen model catalog. When false, report "
            "nothing (there is no static list). Requires the API key.",
        },
        "all": {
            "type": "boolean",
            "default": False,
            "description": "Include the non-reasoning models (audio/TTS/image) too. Default "
            "false — only the reasoning LLMs.",
        },
    },
    "required": [],
    "additionalProperties": False,
}

_LIST_ATLAS_SCHEMA = {
    "type": "object",
    "properties": {
        "refresh": {
            "type": "boolean",
            "default": True,
            "description": "Fetch the live Atlas Cloud text-model catalog (no auth needed). "
            "When false, only report the effort choices (no network).",
        },
        "task": {
            "type": "string",
            "minLength": 3,
            "description": "Optional job to rank the live Atlas catalog for, such as "
            "'debug a large Rust repository' or 'cheap low-latency support chat'.",
        },
        "limit": {
            "type": "integer",
            "minimum": 2,
            "maximum": 8,
            "default": 5,
            "description": "Maximum task-matched models to offer.",
        },
        "interactive": {
            "type": "boolean",
            "default": True,
            "description": "When a task is supplied, open a native model + effort picker if "
            "the MCP client supports form elicitation; otherwise return picker JSON.",
        },
    },
    "required": [],
    "additionalProperties": False,
}

# The consolidated catalogue tool — one listing for the gateway providers,
# replacing list_ali_models / list_atlas_models / list_openrouter_models /
# list_ollama_models / list_lms_models (kept callable as unadvertised aliases).
_LIST_MODELS_SCHEMA = {
    "type": "object",
    "properties": {
        "provider": {
            "type": "string",
            "enum": ["ali", "atlas", "openrouter", "ollama", "lmstudio"],
            "description": "Which catalogue to list: 'ali' (Alibaba/Qwen reasoning), "
            "'atlas' (Atlas Cloud text models), 'openrouter' (~400 models, one key), "
            "'ollama' (cloud catalog + locally pulled + configured council), or "
            "'lmstudio' (the local server: loaded/available models and VRAM fit).",
        },
        "refresh": {
            "type": "boolean",
            "default": True,
            "description": "Fetch the live catalog. When false, report only the "
            "configured defaults (no network).",
        },
        "task": {
            "type": "string",
            "minLength": 3,
            "description": "Atlas/OpenRouter only: an optional job to rank the "
            "catalogue for, e.g. 'debug a large Rust repository'.",
        },
        "limit": {
            "type": "integer",
            "minimum": 2,
            "maximum": 8,
            "default": 5,
            "description": "Atlas/OpenRouter only: maximum task-matched models to offer.",
        },
        "interactive": {
            "type": "boolean",
            "default": True,
            "description": "Atlas/OpenRouter only: with a task, open a native model "
            "picker if the MCP client supports form elicitation.",
        },
        "all": {
            "type": "boolean",
            "default": False,
            "description": "Ali only: include the non-reasoning (audio/TTS/image) "
            "models too.",
        },
    },
    "required": ["provider"],
    "additionalProperties": False,
}

_ATLAS_COUNCIL_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask several Atlas Cloud models; "
        "the adjudicator (GPT-5.6 Sol by default) then synthesizes their answers into one. "
        "Panelists use the configured Atlas effort and are subject to Atlas's ~242s gateway "
        "cutoff (HTTP 504 on longer generations) — lower `atlas_effort` or pin "
        "`atlas_max_tokens` / `atlas_timeout` when panels fail that way.",
        context_desc=(
            "Optional code snippets, file paths, or structural context (shared by all models)."
        ),
        session=_HUB_SESSION_PROP,
        models={
            "type": "array",
            "items": {"type": "string"},
            "description": "Atlas Cloud model ids (e.g. ['zai-org/glm-5.2', "
            "'deepseek-ai/deepseek-v4-pro']); an 'atlas:' prefix is optional. Omit to use "
            "the configured set (configure_atlas_council / ASK_FABLE_ATLAS_COUNCIL), else "
            "3 featured catalog models, one per provider. Requires an Atlas API key on the "
            "server (xai/grok-* members can reroute to the local grok CLI without one).",
        },
        synthesizer={
            "anyOf": [
                {"type": "string", "enum": list(oracles.KNOWN) + sorted(oracles.ALIASES)},
                {"type": "string", "pattern": "^ollama:.+"},
                {"type": "string", "pattern": "^atlas:.+"},
                {"type": "string", "pattern": "^openrouter:.+"},
                {"type": "string", "pattern": "^ali:.+"},
            ],
            "description": "Model that reconciles the panel answers into one. Default "
            "ladder: the local codex CLI (GPT-5.6 Sol) when installed → "
            "'atlas:openai/gpt-5.6-sol' when Atlas is configured → 'fable'. Falls back to "
            "Fable when the pick is unavailable or fails (see `synthesis` in the result).",
        },
    )
)

_OPENROUTER_COUNCIL_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask several OpenRouter models; "
        "the adjudicator (GPT-5.6 Sol by default) then synthesizes their answers into one.",
        context_desc=(
            "Optional code snippets, file paths, or structural context (shared by all models)."
        ),
        session=_HUB_SESSION_PROP,
        models={
            "type": "array",
            "items": {"type": "string"},
            "description": "OpenRouter model ids (e.g. ['anthropic/claude-fable-5.1', "
            "'deepseek/deepseek-v4.1-flash']); an 'openrouter:' prefix is optional. Omit to use "
            "the configured set (configure_openrouter_council / ASK_FABLE_OPENROUTER_COUNCIL), "
            "else 3 featured catalog models, one per provider. A panel spanning several labs "
            "is the point — one key, genuinely different reasoners.",
        },
        synthesizer={
            "anyOf": [
                {"type": "string", "enum": list(oracles.KNOWN) + sorted(oracles.ALIASES)},
                {"type": "string", "pattern": "^ollama:.+"},
                {"type": "string", "pattern": "^atlas:.+"},
                {"type": "string", "pattern": "^openrouter:.+"},
                {"type": "string", "pattern": "^ali:.+"},
            ],
            "description": "Model that reconciles the panel answers into one. Default "
            "ladder: the local codex CLI (GPT-5.6 Sol) when installed → "
            "'openrouter:openai/gpt-5.6-sol' when OpenRouter is configured → 'fable'.",
        },
    )
)

_CONFIGURE_OPENROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "models": {
            "type": "array",
            "items": {"type": "string"},
            "description": "OpenRouter model ids to persist as the default council "
            "(e.g. ['anthropic/claude-fable-5.1', 'openai/gpt-5.6-sol', "
            "'deepseek/deepseek-v4.1-flash']). Call `list_openrouter_models` first.",
        },
        "synthesizer": {
            "type": "string",
            "description": "Model that adjudicates the panel — any council token "
            "('codex'/'gpt', 'fable', 'openrouter:<model-id>') or a bare OpenRouter id.",
        },
    },
    "required": [],
    "additionalProperties": False,
}

_CONFIGURE_OLLAMA_SCHEMA = {
    "type": "object",
    "properties": {
        "models": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ollama Cloud model ids for the council (e.g. "
            "['minimax-m3:cloud','glm-5.2:cloud','qwen3-coder:480b-cloud']). An "
            "'ollama:' prefix is optional; a bare name like 'minimax-m3' is "
            "normalized to 'minimax-m3:cloud'. This becomes ask_ollama_council's "
            "default and the `full` tier's Ollama members, persisted across sessions.",
        },
        "default_model": {
            "type": "string",
            "description": "Optional: the single model `ask_ollama` uses when none is "
            "passed (e.g. 'gpt-oss:120b-cloud').",
        },
    },
    "required": [],
    "additionalProperties": False,
}

_CONFIGURE_ATLAS_SCHEMA = {
    "type": "object",
    "properties": {
        "models": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Atlas Cloud model ids for the council (e.g. "
            "['zai-org/glm-5.2','deepseek-ai/deepseek-v4-pro','moonshotai/kimi-k2']). An "
            "'atlas:' prefix is optional. This becomes ask_atlas_council's default, "
            "persisted across sessions. Ground the picks with list_atlas_models first.",
        },
        "synthesizer": {
            "type": "string",
            "description": "Optional: the model ask_atlas_council uses to reconcile the "
            "panel (e.g. 'gpt' for the local GPT-5.6 Sol CLI, 'openai/gpt-5.6-sol' for the "
            "Atlas-hosted one, or 'fable'). Omit to keep the built-in ladder: local codex "
            "CLI → Atlas-hosted GPT-5.6 Sol → Fable.",
        },
    },
    "required": [],
    "additionalProperties": False,
}

# One writer for the provider-scoped council defaults (ollama/atlas/openrouter).
# The legacy configure_*_council tools stay callable as unadvertised aliases.
_CONFIGURE_COUNCIL_SCHEMA = {
    "type": "object",
    "properties": {
        "provider": {
            "type": "string",
            "enum": ["ollama", "atlas", "openrouter"],
            "description": "Which provider's council default to persist: 'ollama', "
            "'atlas', or 'openrouter'.",
        },
        "models": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Model ids to persist as that provider's default council "
            "(bare ids, or provider-prefixed tokens). Ground the picks with "
            "`list_models(provider=…)` first.",
        },
        "synthesizer": {
            "type": "string",
            "description": "Atlas/OpenRouter only: the adjudicator for the panel "
            "('codex'/'gpt', 'fable', a bare model id, or a provider token). Omit to "
            "keep the built-in GPT-first ladder.",
        },
        "default_model": {
            "type": "string",
            "description": "Ollama only: the single model `ask_model(provider=\"ollama\")` "
            "uses when none is passed (e.g. 'gpt-oss:120b-cloud').",
        },
    },
    "required": ["provider"],
    "additionalProperties": False,
}

_CONFIGURE_TRACING_SCHEMA = {
    "type": "object",
    "properties": {
        "trace_mode": {
            "type": "string",
            "enum": ["safe", "full"],
            "description": "'full' captures redacted model reasoning into traces and "
            "trace bundles (and saves answer markdown); 'safe' withholds reasoning "
            "content while structural traces still record. Persisted; overrides "
            "ASK_FABLE_TRACE_MODE. Takes effect on the next call — no restart.",
        },
        "stream_reasoning": {
            "type": "boolean",
            "description": "Stream model thinking live to the ask_fable console as "
            "calls run (true) or off (false). Persisted; overrides "
            "ASK_FABLE_STREAM_REASONING. Streams to the server's own console, not "
            "into this tool result.",
        },
    },
    "required": [],
    "additionalProperties": False,
}

_CONFIGURE_DISABLED_SCHEMA = {
    "type": "object",
    "properties": {
        "disable": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tokens to ADD to the denylist. An oracle key or alias "
            "('grok', 'codex', 'm3', 'opus48') disables that one; a provider name "
            "('atlas', 'openrouter', 'ollama', 'lmstudio') disables all of its "
            "models at once. Disabled backends are dropped from every council and "
            "their dedicated tool returns kind='disabled'.",
        },
        "enable": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tokens to REMOVE from the denylist (re-enable).",
        },
        "set": {
            "type": "array",
            "items": {"type": "string"},
            "description": "REPLACE the whole denylist with exactly these tokens "
            "(wins over `disable`/`enable`). Pass [] to remove the config override; "
            "if an ASK_FABLE_DISABLED env var is set it then applies again (the "
            "result's `disabled` shows the effective list either way).",
        },
    },
    "required": [],
    "additionalProperties": False,
}

_CONTEXT_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "description": "Stable key to store this context under (e.g. 'repo:auth', "
            "'ticket-431/stacktrace'). Reusing a key overwrites it.",
        },
        "value": {
            "type": "string",
            "description": "The context to store — code, file contents, a stack trace, design "
            "notes. Paste it ONCE here, then reference it by key via `context_ref` on `ask`.",
        },
        "description": {
            "type": "string",
            "default": "",
            "description": "Optional one-line note about what this holds (shown in context_list).",
        },
    },
    "required": ["key", "value"],
    "additionalProperties": False,
}

_CODE_INDEX_SCHEMA = {
    "type": "object",
    "properties": {
        "rebuild": {
            "type": "boolean",
            "description": "Re-chunk and re-embed every file even when its content hash is "
            "unchanged (default false: incremental).",
        },
    },
    "additionalProperties": False,
}

_CODE_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Natural-language or code search query.",
        },
        "k": {
            "type": "integer",
            "description": "Maximum hits to return (default 8, max 50).",
        },
        "rerank": {
            "type": "boolean",
            "description": "Reorder the top hits with the chat model in "
            "`ASK_FABLE_EMBED_RERANK_MODEL` (opt-in; skipped with a note when unset or "
            "unreachable).",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_CONTEXT_PACK_SCHEMA = {
    "type": "object",
    "properties": {
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "File specs to read from the configured project root — each a path "
            "relative to that root, optionally with a 1-indexed inclusive line range as "
            "`path:START-END` (e.g. 'src/app/db.py' or 'src/app/db.py:40-80').",
        },
        "key": {
            "type": "string",
            "description": "Stable key to store the packed bundle under; then pass "
            "context_ref='<key>' on `ask`. Reusing a key overwrites it.",
        },
        "max_chars": {
            "type": "integer",
            "description": "Optional cap on total packed characters (default ~24000). Files "
            "that don't fit are reported in `skipped`, never silently truncated.",
        },
    },
    "required": ["paths", "key"],
    "additionalProperties": False,
}

_CONTEXT_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "description": "Key to read back. Omit to LIST every stored key (with its size, "
            "age and description) — the discovery call before re-pasting context.",
        },
    },
    "required": [],
    "additionalProperties": False,
}

# The mutating half of the context bus (write/pack/delete). Kept separate from
# `context_read` so the read tool stays read-only (a client can auto-approve it)
# while these are honestly flagged destructive.
_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["write", "pack", "delete"],
            "description": "'write' stores `value` under `key`; 'pack' reads the repo files "
            "in `paths` and stores the bundle under `key`; 'delete' removes `key`.",
        },
        "key": {"type": "string", "description": "The context key (required for every op)."},
        "value": {
            "type": "string",
            "description": "op='write': the context to store — code, file contents, a stack "
            "trace, design notes. Paste it ONCE, then reference it by key via `context_ref`.",
        },
        "description": {
            "type": "string",
            "default": "",
            "description": "op='write': optional one-line note about what this holds.",
        },
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "op='pack': file specs relative to the configured project root, "
            "each a path or `path:START-END` (1-indexed inclusive).",
        },
        "max_chars": {
            "type": "integer",
            "description": "op='pack': optional cap on total packed characters (default ~24000).",
        },
    },
    "required": ["op", "key"],
    "additionalProperties": False,
}

_HELP_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "enum": ["refused", "context", "setup", "tools", "all"],
            "description": (
                "Which part of the manual to return. `refused`: what to do with a "
                'status:"refused" result (reframe, never resend). '
                "`context`: the shared context bus — paste once, reference by key. "
                "`setup`: configuring Ollama/Atlas/OpenRouter councils. `tools`: the full "
                "tool menu and the model tokens usable in councils/chains/debates. "
                "Defaults to `all`."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}

_CONTEXT_LIST_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

_CONTEXT_DELETE_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "Key to delete."},
    },
    "required": ["key"],
    "additionalProperties": False,
}

_RESET_SCHEMA = {
    "type": "object",
    "properties": {
        "session": {"type": "string", "default": "default", "description": "Session key to clear."},
        "model": {
            "type": "string",
            # Exactly the spellings `ask(oracle=…)` accepts (every Opus-family alias
            # included), so any session `ask` can open, reset can clear — they all
            # name the one Opus namespace. A hand-kept copy had drifted (no opus48).
            "enum": list(_ASK_ORACLE_SPELLINGS),
            "default": "fable",
            "description": "Which tool's conversation to clear: 'fable' for `ask`, 'opus5' "
            "(or 'opus') for "
            "`ask_opus5`. The two tools namespace their sessions separately, so the same "
            "key names two independent conversations.",
        },
        "save": {
            "type": "boolean",
            "default": True,
            "description": "Write the transcript to a file before clearing.",
        },
    },
    "required": [],
    "additionalProperties": False,
}

_STATS_SCHEMA = {
    "type": "object",
    "properties": {
        "window": {
            "type": "string",
            "enum": list(stats.WINDOWS),
            "default": "24h",
            "description": "How far back to aggregate: '1h', '24h', '7d', or 'all'.",
        },
        "by": {
            "type": "string",
            "enum": list(stats.BY),
            "default": "model",
            "description": (
                "Bucket key. 'model' attributes a call to the model that answered (a "
                "council to its synthesizer); 'provider' is per backend call — the only "
                "view that sees council/chain/debate members one by one, and what the "
                "circuit breaker shed (circuit_open); also 'tool', 'session', 'day', "
                "'project', 'cache', 'mode'."
            ),
        },
        "model": {"type": "string", "description": "Only include records for this model label."},
        "session": {"type": "string", "description": "Only include records for this session key."},
    },
    "required": [],
    "additionalProperties": False,
}

_TRACE_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        "tool": {"type": "string"},
        "status": {"type": "string"},
        "provider": {"type": "string"},
        "session": {"type": "string"},
        "project": {"type": "string"},
        "before": {"type": "string"},
    },
    "additionalProperties": False,
}

_TRACE_GET_SCHEMA = {
    "type": "object",
    "properties": {
        "trace_id": {"type": "string"},
        "include_content": {"type": "boolean", "default": False},
        "max_chars": {"type": "integer", "minimum": 1, "maximum": 50000, "default": 4000},
    },
    "required": ["trace_id"],
    "additionalProperties": False,
}

_SESSION_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "all_projects": {
            "type": "boolean",
            "default": False,
            "description": "Show sessions from ALL projects on this machine, not just the current one.",
        },
        "active_only": {
            "type": "boolean",
            "default": True,
            "description": (
                "Only sessions with a recent heartbeat (not stale). Default true so "
                "the dashboard shows live work. Pass false to include retained history."
            ),
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
    },
    "additionalProperties": False,
}

_SESSION_PEEK_SCHEMA = {
    "type": "object",
    "properties": {
        "session_key": {
            "type": "string",
            "description": "The session label to inspect.",
        },
        "agent_id": {
            "type": "string",
            "description": "Optional: restrict to one agent's turns on that session.",
        },
    },
    "required": ["session_key"],
    "additionalProperties": False,
}

_SESSION_STATS_SCHEMA = {
    "type": "object",
    "properties": {
        "all_projects": {
            "type": "boolean",
            "default": False,
            "description": "Aggregate across all projects on this machine, not just the current one.",
        },
        "window_s": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2592000,
            "default": 86400,
            "description": (
                "Only count turns from the last N seconds. Default 86400 (24h). "
                "Pass 0 for all retained history."
            ),
        },
    },
    "additionalProperties": False,
}

_TOOL_SCHEMAS = {
    "ask": _ASK_SCHEMA,
    "ask_model": _MODEL_SCHEMA,
    "ask_opus5": _OPUS_SCHEMA,
    "ask_sonnet": _SONNET_SCHEMA,
    "ask_m3": _M3_SCHEMA,
    "ask_glm": _GLM_SCHEMA,
    "ask_deepseek": _DEEPSEEK_SCHEMA,
    "ask_gemini": _GEMINI_SCHEMA,
    "ask_codex": _CODEX_SCHEMA,
    "ask_grok": _GROK_SCHEMA,
    "ask_kimi": _KIMI_SCHEMA,
    "ask_websearch": _WEBSEARCH_SCHEMA,
    "ask_council": _COUNCIL_SCHEMA,
    "ask_chain": _CHAIN_SCHEMA,
    "ask_debate": _DEBATE_SCHEMA,
    "ask_falsify": _FALSIFY_SCHEMA,
    "ask_verify": _VERIFY_SCHEMA,
    "ask_conference": _CONFERENCE_SCHEMA,
    "ask_ollama": _OLLAMA_SCHEMA,
    "ask_ollama_council": _OLLAMA_COUNCIL_SCHEMA,
    "list_ollama_models": _LIST_OLLAMA_SCHEMA,
    "list_models": _LIST_MODELS_SCHEMA,
    "ask_ali": _ALI_SCHEMA,
    "list_ali_models": _LIST_ALI_SCHEMA,
    "ask_atlas": _ATLAS_SCHEMA,
    "ask_openrouter": _OPENROUTER_SCHEMA,
    "ask_openrouter_council": _OPENROUTER_COUNCIL_SCHEMA,
    "configure_openrouter_council": _CONFIGURE_OPENROUTER_SCHEMA,
    "configure_council": _CONFIGURE_COUNCIL_SCHEMA,
    "list_openrouter_models": _LIST_OPENROUTER_SCHEMA,
    "ask_atlas_council": _ATLAS_COUNCIL_SCHEMA,
    "list_atlas_models": _LIST_ATLAS_SCHEMA,
    "configure_ollama_council": _CONFIGURE_OLLAMA_SCHEMA,
    "configure_atlas_council": _CONFIGURE_ATLAS_SCHEMA,
    "configure_tracing": _CONFIGURE_TRACING_SCHEMA,
    "configure_disabled": _CONFIGURE_DISABLED_SCHEMA,
    "code_index": _CODE_INDEX_SCHEMA,
    "code_search": _CODE_SEARCH_SCHEMA,
    "context_write": _CONTEXT_WRITE_SCHEMA,
    "context_pack": _CONTEXT_PACK_SCHEMA,
    "context_read": _CONTEXT_READ_SCHEMA,
    "context": _CONTEXT_SCHEMA,
    "context_list": _CONTEXT_LIST_SCHEMA,
    "ask_fable_help": _HELP_SCHEMA,
    "context_delete": _CONTEXT_DELETE_SCHEMA,
    "reset_session": _RESET_SCHEMA,
    "stats": _STATS_SCHEMA,
    "trace_list": _TRACE_LIST_SCHEMA,
    "trace_get": _TRACE_GET_SCHEMA,
    "session_list": _SESSION_LIST_SCHEMA,
    "session_peek": _SESSION_PEEK_SCHEMA,
    "session_stats": _SESSION_STATS_SCHEMA,
    "ask_lms": _LMS_SCHEMA,
    "ask_lms_council": _LMS_COUNCIL_SCHEMA,
    "list_lms_models": _LIST_LMS_SCHEMA,
    "unload_lms_model": _UNLOAD_LMS_SCHEMA,
    "host_status": _HOST_STATUS_SCHEMA,
    "diagnose": _DIAGNOSE_SCHEMA,
}


# Legacy tool names, kept callable (but unadvertised) after the consolidation.
# Each maps to (advertised tool, arguments it implies); a caller-supplied value
# for an injected key wins.
_LEGACY_TOOL_ALIASES: dict[str, tuple[str, dict]] = {
    "ask_sonnet": ("ask_model", {"provider": "sonnet"}),
    "ask_m3": ("ask_model", {"provider": "minimax"}),
    "ask_glm": ("ask_model", {"provider": "glm"}),
    "ask_deepseek": ("ask_model", {"provider": "deepseek"}),
    "ask_gemini": ("ask_model", {"provider": "gemini"}),
    "ask_codex": ("ask_model", {"provider": "codex"}),
    "ask_grok": ("ask_model", {"provider": "grok"}),
    "ask_kimi": ("ask_model", {"provider": "kimi"}),
    "ask_ollama": ("ask_model", {"provider": "ollama"}),
    "ask_lms": ("ask_model", {"provider": "lmstudio"}),
    "ask_atlas": ("ask_model", {"provider": "atlas"}),
    "ask_ali": ("ask_model", {"provider": "ali"}),
    "ask_openrouter": ("ask_model", {"provider": "openrouter"}),
    "list_ali_models": ("list_models", {"provider": "ali"}),
    "list_atlas_models": ("list_models", {"provider": "atlas"}),
    "list_openrouter_models": ("list_models", {"provider": "openrouter"}),
    "list_ollama_models": ("list_models", {"provider": "ollama"}),
    "list_lms_models": ("list_models", {"provider": "lmstudio"}),
    "ask_opus5": ("ask", {"oracle": "opus"}),
    "ask_ollama_council": ("ask_council", {"provider": "ollama"}),
    "ask_atlas_council": ("ask_council", {"provider": "atlas"}),
    "ask_openrouter_council": ("ask_council", {"provider": "openrouter"}),
    "ask_lms_council": ("ask_council", {"provider": "lmstudio"}),
    "configure_ollama_council": ("configure_council", {"provider": "ollama"}),
    "configure_atlas_council": ("configure_council", {"provider": "atlas"}),
    "configure_openrouter_council": ("configure_council", {"provider": "openrouter"}),
    "context_write": ("context", {"op": "write"}),
    "context_pack": ("context", {"op": "pack"}),
    "context_delete": ("context", {"op": "delete"}),
    "context_list": ("context_read", {}),
}


def _resolve_tool_alias(name: str, arguments: dict) -> tuple[str, dict]:
    """Fold a legacy tool name into its consolidated tool.

    ``ask_m3`` becomes ``ask_model`` with ``provider="minimax"``;
    ``list_atlas_models`` becomes ``list_models`` with ``provider="atlas"``;
    ``ask_opus5`` becomes ``ask`` with ``oracle="opus"``. The legacy names are no
    longer advertised but stay callable, so a client that cached the old tool list
    (or a skill/bookmark) keeps working."""
    target = _LEGACY_TOOL_ALIASES.get(name)
    if target is None:
        return name, arguments
    new_name, implied = target
    merged = dict(arguments)
    for key, value in implied.items():
        merged.setdefault(key, value)
    return new_name, merged


def _schema_error(name: str, arguments: dict) -> str | None:
    schema = _TOOL_SCHEMAS.get(name)
    if schema is None:
        return None
    required = schema.get("required") or []
    for field in required:
        if field not in arguments:
            return f"missing required argument: {field}"
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        unexpected = sorted(set(arguments) - set(properties))
        if unexpected:
            return f"unexpected argument: {unexpected[0]}"
    expected_types = {"string": str, "array": list, "object": dict, "boolean": bool}
    for field, value in arguments.items():
        spec = properties.get(field) or {}
        kind = spec.get("type")
        any_of = spec.get("anyOf")
        if kind == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif kind is None and any_of:
            # anyOf field (e.g. context_ref = string | array): the value must match at
            # least one alternative's declared type. A member with no "type" — a
            # pattern/enum-only string schema — is treated as accepting strings.
            # Without this branch a None top-level type made `valid` default True for
            # ANY JSON type, so context_ref={"k":1} / 123 passed validation and was
            # then silently dropped as "no keys" — the model answered blind.
            allowed: set[type] = set()
            for alt in any_of:
                at = alt.get("type")
                if at == "integer":
                    allowed.add(int)
                elif at in expected_types:
                    allowed.add(expected_types[at])
                elif at is None:
                    allowed.add(str)
            valid = (not allowed) or any(
                isinstance(value, t) and not (t is int and isinstance(value, bool)) for t in allowed
            )
        else:
            expected = expected_types.get(kind)
            valid = expected is None or isinstance(value, expected)
        if not valid:
            return f"invalid type for argument: {field}"
        if "minimum" in spec and value < spec["minimum"]:
            return f"invalid value for argument: {field}"
        if "maximum" in spec and value > spec["maximum"]:
            return f"invalid value for argument: {field}"
        if "minLength" in spec and len(value) < spec["minLength"]:
            return f"invalid value for argument: {field}"
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            return f"invalid value for argument: {field}"
        if "enum" in spec and value not in spec["enum"]:
            return f"invalid value for argument: {field}"
    return None


def _text(payload: dict) -> types.TextContent:
    payload = trace_bundle.redact_value(payload)
    trace = trace_runtime.current()
    if trace is not None:
        if payload.get("kind") in {"bad_args", "unknown_tool"}:
            trace_runtime.record_stage("validation", "error")
        if payload.get("status") in {"needs_context", "context_exhausted"}:
            trace_runtime.record_stage("context", "needs_context")
        payload = trace.complete(payload)
    return types.TextContent(type="text", text=json.dumps(payload))


def _cache_lookup(
    tool: str, models: list[str], question: str, context: str, effort: str | None = None
):
    """Return (cache_key, served_payload_or_None). A served payload is the stored
    answer decorated with ``cached``/``cache_age_s``/``note`` so the caller can
    return it verbatim."""
    ck = cache.key(tool, models, question, context, effort=effort)
    hit = cache.get(ck)
    if hit is None:
        trace_runtime.record_stage(
            "cache.outer",
            "miss",
            kind=trace_runtime.EventKind.CACHE,
            cache={"status": "miss", "layer": "outer"},
        )
        return ck, None
    payload, age = hit
    trace_runtime.record_stage(
        "cache.outer",
        "hit",
        kind=trace_runtime.EventKind.CACHE,
        cache={"status": "hit", "layer": "outer", "age_ms": age * 1000},
    )
    served = trace_runtime.prepare_cache_hit(payload, age_seconds=age)
    served["note"] = (
        f"served from cache — a near-identical question was answered {age}s ago "
        "(ASK_FABLE_CACHE=0 to disable, ASK_FABLE_CACHE_TTL to tune the window)"
    )
    return ck, served


def _trusted_allowed() -> bool:
    """Whether a caller's ``trusted`` flag may actually lift the prohibited-use
    denylist. OFF unless the OPERATOR opts in via ASK_FABLE_ALLOW_TRUSTED (env or
    config). MCP tool arguments come from the calling agent — the very entity the
    guard constrains — so an ungated flag would BE its own authorization: any caller
    could self-certify and disable Layer 2. This puts the authorization back in the
    operator's hands, where the flag's own description always claimed it was."""
    return (config.setting("ASK_FABLE_ALLOW_TRUSTED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _resolve_trusted(args: dict) -> bool:
    """Effective ``trusted`` for one call: the caller asked AND the operator
    authorized it. Shared by every handler so the gate can't drift — and so a
    ``trusted=true`` on any tool (not just ``ask``) is the one escape hatch from
    the denylist, including the `context` scan."""
    return bool(args.get("trusted") or False) and _trusted_allowed()


def _guard_check(question: str, context: str, *, trusted: bool = False) -> tuple[bool, str]:
    if trusted:
        allowed, reason = guard.check(question, context, trusted=True)
    else:
        allowed, reason = guard.check(question, context)
    trace_runtime.record_stage("guard", "ok" if allowed else "refused")
    return allowed, reason


# The guard is deterministic — it never consults a model — so resending a refused
# question just re-refuses it. The reframe recipe used to live in the server's
# standing instructions, which harnesses truncate before an agent ever reads it;
# carrying it on the refusal itself puts it in front of the agent at the one
# moment it is actionable.
def _guard_refusal(reason: str) -> dict:
    """Refusal payload for a Layer-2 (deterministic denylist) block: names the
    field that tripped (`question` or `context`) and carries the reframe recipe."""
    in_context = reason.endswith("(context)")
    return {
        "status": "refused",
        "stage": "guard",
        "reason": reason,
        "where": "context" if in_context else "question",
        "how_to_reframe": (
            "This refusal is DETERMINISTIC — resending the same question refuses "
            "again. REFRAME instead: ask the underlying engineering question about "
            "a named symbol (the `where` field names what tripped it: `question` or "
            "`context`). OR pass `trusted=true` for authorized work — it takes "
            "effect only when the operator has set ASK_FABLE_ALLOW_TRUSTED."
        )
        + ' Call `ask_fable_help("refused")` for the full recipe.',
    }


def _model_refused_payload(res) -> dict:
    """The refused payload for a MODEL-stage refusal.

    A plain model refusal (the ``REFUSED:`` text contract) is byte-identical to
    before. A PROVIDER-safeguard refusal (``kind == "provider_refusal"``, stamped
    by the bridge) carries the reframe recipe, because its remedy is different
    from the guard's: it is not deterministic, and the fix is a backend swap —
    never resending the same question."""
    payload: dict = {"status": "refused", "stage": "model", "reason": res.text}
    if getattr(res, "kind", "") == "provider_refusal":
        payload["how_to_reframe"] = _provider_refusal_recipe()
    return payload


def _provider_refusal_recipe() -> str:
    return (
        "This refusal came from the PROVIDER's own safeguard, not our guard — so "
        "unlike a guard refusal it is NOT deterministic: the same payload may pass "
        "on another backend. Try a different "
        "backend (`ask_websearch` with model=\"grok\", or a non-Anthropic oracle). "
        "Do NOT resend the same question unchanged — and note the provider reads the "
        "WHOLE payload, `context` included. For account-level relief see Anthropic's "
        "Cyber Verification Program."
    )


# Parent-process basenames → stable hub agent labels (when clientInfo is empty).
_PARENT_AGENT_MAP = {
    "claude": "claude-code",
    "grok": "grok",
    "opencode": "opencode",
    "codex": "codex",
    "cursor": "cursor",
    "cursor-agent": "cursor",
}
_unknown_agent_warned = False


def _agent_id_from_env() -> str | None:
    """Harness env fallbacks when MCP clientInfo is missing or empty.

    Claude Code exports ``CLAUDECODE=1`` / ``CLAUDE_CODE_*``; Grok may set
    ``GROK_AGENT``; Codex often has ``CODEX_THREAD_ID`` / ``CODEX_*``. These are
    coarser than ``ASK_FABLE_AGENT_ID`` but beat a silent ``unknown``.
    """
    if (os.environ.get("CLAUDECODE") or "").strip() in ("1", "true", "yes"):
        return "claude-code"
    if (os.environ.get("CLAUDE_CODE_ENTRYPOINT") or "").strip():
        return "claude-code"
    ai = (os.environ.get("AI_AGENT") or "").strip().lower()
    if "claude" in ai:
        return "claude-code"
    if (os.environ.get("GROK_AGENT") or "").strip() in ("1", "true", "yes"):
        return "grok"
    # Codex CLI / appserver threads (avoid bare CODEX_HOME — often set globally).
    if (os.environ.get("CODEX_THREAD_ID") or "").strip():
        return "codex"
    if (os.environ.get("CODEX_CI") or "").strip() in ("1", "true", "yes"):
        return "codex"
    if (os.environ.get("OPENCODE_XATS_BASE_URL") or "").strip():
        return "opencode"
    return None


def _agent_id_from_parent() -> str | None:
    """Last-resort: map the MCP host process name (ppid) to a harness label."""
    try:
        ppid = os.getppid()
        if ppid <= 1:
            return None
        # Prefer /proc/.../comm (single token); fall back to cmdline argv0 basename.
        comm = Path(f"/proc/{ppid}/comm").read_text(encoding="utf-8", errors="replace").strip()
        base = comm.split("/")[-1].lower() if comm else ""
        if not base:
            raw = Path(f"/proc/{ppid}/cmdline").read_bytes().split(b"\0", 1)[0]
            base = Path(raw.decode("utf-8", errors="replace")).name.lower()
        if not base:
            return None
        if base in _PARENT_AGENT_MAP:
            return _PARENT_AGENT_MAP[base]
        # e.g. "grok-shell" → try first path segment before hyphen for known hosts
        head = base.split("-", 1)[0]
        return _PARENT_AGENT_MAP.get(head)
    except (OSError, ValueError):
        return None


def _agent_id(server: Server, *, allow_explicit: bool = True) -> str:
    """Identity of the connecting MCP client (hub attribution).

    Resolution order:
      1. ``ASK_FABLE_AGENT_ID`` (explicit, highest priority — use for per-window names)
      2. MCP InitializeRequest ``clientInfo.name`` (Claude Code / opencode / grok-shell / …)
      3. Harness env hints (``CLAUDECODE``, ``GROK_AGENT``, ``CODEX_*``, …)
      4. Parent process basename (``claude`` → ``claude-code``, ``grok`` → ``grok``, …)
      5. ``"unknown"`` (warn once on stderr)

    ``allow_explicit=False`` skips step 1 to return the *harness label* alone
    (clientInfo/env/parent). The context-bus writer uses that: a blob should be
    attributed to its client harness — a bounded, auto-detected set — not to an
    arbitrary per-window ``ASK_FABLE_AGENT_ID``, which would both misrepresent the
    "client" and let a high-cardinality label defeat the daemon's per-writer cap.

    Read lazily inside each tool call (init always strictly precedes the first
    tools/call).
    """
    if allow_explicit:
        explicit = (os.environ.get("ASK_FABLE_AGENT_ID") or "").strip()
        if explicit:
            return explicit
    try:
        params = server.request_context.session.client_params
        if params is not None and params.clientInfo is not None:
            name = (params.clientInfo.name or "").strip()
            # Treat empty / literal "unknown" as missing so env/parent can recover.
            if name and name.lower() != "unknown":
                return name
    except (LookupError, AttributeError):
        pass
    for hint_source in (_agent_id_from_env, _agent_id_from_parent):
        try:
            hinted = hint_source()
        except Exception:  # noqa: BLE001 — attribution must never break a turn
            hinted = None
        if hinted:
            return hinted
    global _unknown_agent_warned
    if not _unknown_agent_warned:
        _unknown_agent_warned = True
        print(
            "ask_fable: agent_id unresolved (set ASK_FABLE_AGENT_ID, or ensure the "
            "MCP client sends clientInfo.name); hub rows will use 'unknown'",
            file=sys.stderr,
        )
    return "unknown"


# Per-call agent id — set at the top of each call_tool invocation, read inside the
# ask handlers so they can attribute hub writes without agent_id threaded through
# every signature. A ContextVar (the MCP SDK's own pattern for request_ctx) copies
# correctly across the awaits inside a single turn and isolates concurrent calls.
_CALL_AGENT_ID: ContextVar[str] = ContextVar("ask_fable_call_agent_id", default="unknown")


def _hub_mirror(
    *,
    session_key: str,
    question: str,
    answer: str,
    oracle: str,
    status: str,
    sdk_session_id: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Mirror one completed turn into the cross-instance hub. Best-effort, post-hoc.

    Called ONLY from success paths (``ask``, single-oracle, council, chain, debate,
    conference) AFTER the oracle(s) answered — so this can never influence an oracle's answer
    (the hard no-leakage boundary). The hub is visibility-only: it never feeds back
    into the ask path. Refused/error turns and tool-level cache hits are not
    mirrored; a successful result can still have come from an underlying
    per-oracle cache."""
    try:
        hub.write_turn(
            agent_id=_CALL_AGENT_ID.get(),
            project=trace_runtime.project_fingerprint(),
            session_key=session_key or "default",
            question=question,
            answer=answer,
            oracle=oracle,
            status=status,
            sdk_session_id=sdk_session_id,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001 — the hub is best-effort; never break a turn
        pass


def _council_envelope(
    ok: list,
    requested: int,
    synthesizer: str | None,
    *,
    consensus: str | None = None,
    material_disagreement: bool = False,
    partial: list[str] | None = None,
) -> dict:
    """Deterministic council metadata so a caller can SEE when the council degraded
    or disagreed — not only when fewer models answered than were asked.

    ``recommended_next_action`` consults quorum first for thin panels, then
    material disagreement / consensus, then degradation, so a full-quorum
    divergent panel never claims "safe to act".
    """
    n = len(ok)
    # Distinct training lineages among the models that answered. Two members from
    # one lab share pretraining/RLHF, so their agreement is not independent
    # evidence — a 4-Anthropic panel is one opinion wearing four hats.
    labs = oracles.distinct_labs([r.key for r in ok])
    confidence = "high" if n >= 3 else "medium" if n == 2 else "low"
    if n >= 2 and synthesizer is None:
        confidence = "low"  # synthesis failed; this is one raw answer, not a merge
    if n >= 2 and labs < 2 and confidence == "high":
        confidence = "medium"  # unanimous, but one lab — not three independent voices
    cut_off = list(partial or [])
    if cut_off:
        # Some of what was merged is half an answer, so step DOWN one rung. Written as
        # a ladder, not a swap: `"low" if c == "medium" else "medium"` promoted an
        # already-low confidence to medium, so the two worst cases — a lone panelist
        # whose answer was cut off, and a failed synthesis plus a cut-off panelist —
        # came back reading stronger than a clean one.
        confidence = {"high": "medium", "medium": "low", "low": "low"}[confidence]
    degraded = n < requested or bool(cut_off)
    if n <= 1:
        nxt = (
            "only one oracle answered — treat this as a single-model opinion; "
            "re-run or widen `models` if the decision is high-stakes"
        )
    elif material_disagreement or consensus == "divergent":
        nxt = (
            "panelists materially disagreed (apply vs reject/investigate) — "
            "do not treat this as consensus; read sources or escalate to ask_debate"
        )
    elif cut_off:
        # Below material disagreement deliberately: "they disagree" is the more
        # urgent thing to tell a caller than "one answer was cut short".
        nxt = (
            f"{len(cut_off)} of {n} answers ({', '.join(cut_off)}) were cut off at the "
            "output cap — the merged answer rests on partial input; re-run with a "
            "higher token cap or a narrower question before relying on it"
        )
    elif labs < 2:
        nxt = (
            f"all {n} answers came from one lab (shared training lineage) — their "
            "agreement is not independent; widen `models` across labs before treating "
            "this as consensus"
        )
    elif synthesizer is None:
        nxt = (
            "synthesis failed, so the answer is one panelist's raw reply, not a merged "
            "verdict — read the other sources before acting on it"
        )
    elif degraded:
        nxt = (
            f"only {n} of {requested} models answered — weigh the missing ones "
            "before relying on this as consensus"
        )
    elif consensus == "partial":
        nxt = (
            "panel agreement is only partial (mixed recommendations, incomplete "
            "sidecar coverage, or low confidence) — weigh sources before acting"
        )
    elif consensus == "unknown":
        nxt = "consensus signal is unknown — treat as provisional; inspect sources before acting"
    else:
        nxt = (
            "full quorum answered and was synthesized — safe to act on if it "
            "matches your own reasoning"
        )
    return {
        "effective_models": [r.model for r in ok],
        "quorum": f"{n}/{requested}",
        "independent_labs": labs,
        "degraded": degraded,
        **({"partial": cut_off} if cut_off else {}),
        "confidence": confidence,
        "recommended_next_action": nxt,
    }


def _add_panel_gaps(
    payload: dict, unknown: list[str], disabled: list[str] | None = None
) -> dict:
    """Attach the seats a council/chain asked for but did not run: ``unknown``
    tokens nothing recognized (they count as requested, so the result reads as
    degraded) and ``disabled`` members the operator's denylist dropped (they
    don't). Stored in the cache entry too, whose key carries both, so a hit
    reports them exactly as the original answer did."""
    if unknown:
        payload["unknown"] = list(unknown)
    if disabled:
        payload["disabled"] = list(disabled)
    return payload


# --- MCP tool annotations ---------------------------------------------------
# ToolAnnotations are advisory HINTS a host reads to decide whether to auto-run a
# tool or prompt the user first (Claude Code, OpenAI's tool directory). They are
# NOT a security control — the spec says clients must never trust annotations from
# an untrusted server. We set all four on every tool with explicit booleans so no
# host silently falls back to the permissive spec defaults (readOnly=false,
# destructive=true, idempotent=false, openWorld=true).
#
# Axes: readOnlyHint — modifies no state; destructiveHint — may destroy/replace
# existing data (read only when not read-only); idempotentHint — a repeat with the
# same args has no further effect (ditto); openWorldHint — reaches external systems.
#
# Every ask_* model call is readOnly=false on purpose: it appends a trace record
# and bumps stats counters (server state), spends money, and is non-deterministic,
# so hosts should keep prompting rather than auto-run a paid tool. destructive=false
# then distinguishes "will spend / call out" from the default "may destroy".


def _model_call(title: str) -> types.ToolAnnotations:
    # Reaches an external LLM; mutates server state (trace/stats) and costs money.
    return types.ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )


def _reads_local(title: str) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _reads_remote(title: str) -> types.ToolAnnotations:
    # Read-only, but fetches a live catalog from an external service.
    return types.ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )


def _writes_config(title: str) -> types.ToolAnnotations:
    # Persists settings locally; re-running with the same args yields the same state.
    return types.ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _mutates_store(title: str) -> types.ToolAnnotations:
    # Overwrites or clears local store state (INSERT OR REPLACE / delete / clear).
    # destructive=true: can clobber unregenerable user content and there is no undo.
    return types.ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )


# Name -> annotations for every advertised tool. A test asserts this stays in
# lockstep with list_tools() so a newly added tool cannot ship unannotated.
_TOOL_ANNOTATIONS: dict[str, types.ToolAnnotations] = {
    # reasoning / model calls — external LLM, paid, non-idempotent
    "ask": _model_call("Ask Fable"),
    "ask_model": _model_call("Ask one model (by provider)"),
    # Opt-in research agent — reaches the open web (openWorld), spends a turn,
    # non-deterministic: same profile as every other model call.
    "ask_websearch": _model_call("Web-search research agent"),
    "ask_council": _model_call("Ask a model council"),
    "ask_chain": _model_call("Ask a model chain"),
    "ask_debate": _model_call("Ask an adversarial debate"),
    "ask_falsify": _model_call("Run a falsification ledger"),
    "ask_verify": _model_call("Review a draft answer for evidence-backed faults"),
    "ask_conference": _model_call("Ask a model conference"),
    # local static manual — no network, no state
    "ask_fable_help": _reads_local("ask_fable manual"),
    # live external catalog listings — read-only but hit the network
    "list_models": _reads_remote("List a provider's models"),
    "host_status": _reads_remote("Host / GPU status"),
    "diagnose": _reads_remote("Backend health diagnosis"),
    # Destructive remote management: frees a resident model (no undo).
    "unload_lms_model": types.ToolAnnotations(
        title="Unload an LM Studio model",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    # config writers — persist settings to the local config file
    "configure_council": _writes_config("Configure a provider council"),
    "configure_tracing": _writes_config("Configure tracing"),
    "configure_disabled": _writes_config("Enable/disable oracles"),
    # context store writes — overwrite a blob by key (shared keyspace, no undo)
    "code_index": _mutates_store("Index the project for code_search"),
    "code_search": _reads_remote("Search the project index"),
    "context": _mutates_store("Write / pack / delete a context blob"),
    # pure local reads
    "context_read": _reads_local("Read or list the context store"),
    "stats": _reads_local("Usage & health stats"),
    "trace_list": _reads_local("List traces"),
    "trace_get": _reads_local("Read a trace"),
    "session_list": _reads_local("List hub sessions"),
    "session_peek": _reads_local("Peek a hub session"),
    "session_stats": _reads_local("Hub session stats"),
    # destructive local state ops
    "reset_session": _mutates_store("Reset a session"),
}


def build_server() -> Server:
    server: Server = Server("ask_fable", instructions=SERVER_INSTRUCTIONS)
    store = SessionStore()

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools = [
            types.Tool(name="ask", description=ASK_TOOL_DESCRIPTION, inputSchema=_ASK_SCHEMA),
            types.Tool(
                name="ask_fable_help",
                description=ASK_FABLE_HELP_TOOL_DESCRIPTION,
                inputSchema=_HELP_SCHEMA,
            ),
            types.Tool(
                name="ask_model",
                description=ASK_MODEL_TOOL_DESCRIPTION,
                inputSchema=_MODEL_SCHEMA,
            ),
            types.Tool(
                name="ask_websearch",
                description=ASK_WEBSEARCH_TOOL_DESCRIPTION,
                inputSchema=_WEBSEARCH_SCHEMA,
            ),
            types.Tool(
                name="ask_council",
                description=ASK_COUNCIL_TOOL_DESCRIPTION,
                inputSchema=_COUNCIL_SCHEMA,
            ),
            types.Tool(
                name="ask_chain",
                description=ASK_CHAIN_TOOL_DESCRIPTION,
                inputSchema=_CHAIN_SCHEMA,
            ),
            types.Tool(
                name="ask_debate",
                description=ASK_DEBATE_TOOL_DESCRIPTION,
                inputSchema=_DEBATE_SCHEMA,
            ),
            types.Tool(
                name="ask_verify",
                description=ASK_VERIFY_TOOL_DESCRIPTION,
                inputSchema=_VERIFY_SCHEMA,
                title="Review a draft answer",
            ),
            types.Tool(
                name="ask_falsify",
                description=ASK_FALSIFY_TOOL_DESCRIPTION,
                inputSchema=_FALSIFY_SCHEMA,
            ),
            types.Tool(
                name="ask_conference",
                description=ASK_CONFERENCE_TOOL_DESCRIPTION,
                inputSchema=_CONFERENCE_SCHEMA,
            ),
            types.Tool(
                name="configure_council",
                description=CONFIGURE_COUNCIL_TOOL_DESCRIPTION,
                inputSchema=_CONFIGURE_COUNCIL_SCHEMA,
            ),
            types.Tool(
                name="list_models",
                description=LIST_MODELS_TOOL_DESCRIPTION,
                inputSchema=_LIST_MODELS_SCHEMA,
            ),
            types.Tool(
                name="unload_lms_model",
                description=UNLOAD_LMS_MODEL_TOOL_DESCRIPTION,
                inputSchema=_UNLOAD_LMS_SCHEMA,
            ),
            types.Tool(
                name="host_status",
                description=HOST_STATUS_TOOL_DESCRIPTION,
                inputSchema=_HOST_STATUS_SCHEMA,
            ),
            types.Tool(
                name="diagnose",
                description=DIAGNOSE_TOOL_DESCRIPTION,
                inputSchema=_DIAGNOSE_SCHEMA,
            ),
            types.Tool(
                name="configure_tracing",
                description=CONFIGURE_TRACING_TOOL_DESCRIPTION,
                inputSchema=_CONFIGURE_TRACING_SCHEMA,
            ),
            types.Tool(
                name="configure_disabled",
                description=(
                    "Turn oracles/providers OFF (or back on) at runtime — persisted to "
                    "the config file, no restart. A disabled backend is dropped from every "
                    "council/tier and its dedicated tool returns kind='disabled' (distinct "
                    "from 'not configured'). Name an oracle key/alias ('grok', 'm3', "
                    "'opus48') or a whole provider ('atlas', 'openrouter', 'ollama', "
                    "'lmstudio'). Call with no args to see the current denylist. Config "
                    "wins over the ASK_FABLE_DISABLED env var."
                ),
                inputSchema=_CONFIGURE_DISABLED_SCHEMA,
            ),
            types.Tool(
                name="code_index",
                description=CODE_INDEX_TOOL_DESCRIPTION,
                inputSchema=_CODE_INDEX_SCHEMA,
            ),
            types.Tool(
                name="code_search",
                description=CODE_SEARCH_TOOL_DESCRIPTION,
                inputSchema=_CODE_SEARCH_SCHEMA,
            ),
            types.Tool(
                name="context_read",
                description=CONTEXT_READ_TOOL_DESCRIPTION,
                inputSchema=_CONTEXT_READ_SCHEMA,
            ),
            types.Tool(
                name="context",
                description=CONTEXT_TOOL_DESCRIPTION,
                inputSchema=_CONTEXT_SCHEMA,
            ),
            types.Tool(
                name="reset_session",
                description="Dump (optionally to a file) and clear a Fable conversation session, "
                "so the next `ask` on that key starts a fresh topic.",
                inputSchema=_RESET_SCHEMA,
            ),
            types.Tool(
                name="stats",
                description=STATS_TOOL_DESCRIPTION,
                inputSchema=_STATS_SCHEMA,
            ),
            types.Tool(
                name="trace_list",
                description="List recent correlated tool traces without raw content.",
                inputSchema=_TRACE_LIST_SCHEMA,
            ),
            types.Tool(
                name="trace_get",
                description="Read the ordered events and artifact references for one trace.",
                inputSchema=_TRACE_GET_SCHEMA,
            ),
            types.Tool(
                name="session_list",
                description=SESSION_LIST_TOOL_DESCRIPTION,
                inputSchema=_SESSION_LIST_SCHEMA,
            ),
            types.Tool(
                name="session_peek",
                description=SESSION_PEEK_TOOL_DESCRIPTION,
                inputSchema=_SESSION_PEEK_SCHEMA,
            ),
            types.Tool(
                name="session_stats",
                description=SESSION_STATS_TOOL_DESCRIPTION,
                inputSchema=_SESSION_STATS_SCHEMA,
            ),
        ]
        return [t.model_copy(update={"annotations": _TOOL_ANNOTATIONS.get(t.name)}) for t in tools]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        _CALL_AGENT_ID.set(_agent_id(server))
        # Attribute any context blob sealed during this call to its client
        # *harness* — the bounded auto-detected label, never the arbitrary
        # per-window ASK_FABLE_AGENT_ID (which is right for hub rows but would
        # inflate the writer's cardinality and misname the "client").
        context_bus.set_writer_client(_agent_id(server, allow_explicit=False))
        try:
            request_id = str(server.request_context.request_id)
        except LookupError:
            request_id = None
        with trace_runtime.tool_trace(name, arguments or {}, mcp_request_id=request_id):
            try:
                # Legacy per-backend names (ask_m3, list_atlas_models, …) stay
                # callable but are no longer advertised: fold them into the
                # consolidated tool's arguments before validation and dispatch.
                name, arguments = _resolve_tool_alias(name, arguments or {})
                validation_error = _schema_error(name, arguments)
                if validation_error is not None:
                    return [
                        _text({"status": "error", "kind": "bad_args", "detail": validation_error})
                    ]
                if name == "ask_fable_help":
                    return [_text(_handle_help(arguments or {}))]
                if name == "ask":
                    return [_text(await _handle_ask(store, arguments or {}))]
                if name == "ask_opus5":
                    return [_text(await _handle_opus5(store, arguments or {}))]
                if name == "ask_model":
                    return [_text(await _handle_model(arguments or {}))]
                if name == "ask_websearch":
                    return [_text(await _handle_websearch(arguments or {}))]
                if name == "ask_council":
                    return [_text(await _handle_council(arguments or {}))]
                if name == "ask_chain":
                    return [_text(await _handle_chain(arguments or {}))]
                if name == "ask_debate":
                    return [_text(await _handle_debate(arguments or {}))]
                if name == "ask_verify":
                    return [_text(await verify._handle_verify(arguments or {}))]
                if name == "ask_falsify":
                    return [_text(await falsify._handle_falsify(arguments or {}))]
                if name == "ask_conference":
                    conf_args = dict(arguments or {})
                    if bool(conf_args.get("interactive", True)) and not conf_args.get("models"):
                        picked = await _elicit_conference_setup(server, conf_args)
                        if picked.get("action") == "accept":
                            if picked.get("topic"):
                                conf_args["question"] = picked["topic"]
                            if picked.get("models"):
                                conf_args["models"] = picked["models"]
                            if picked.get("rounds"):
                                conf_args["rounds"] = picked["rounds"]
                        elif picked.get("action") in ("decline", "cancel"):
                            return [
                                _text(
                                    {
                                        "status": "cancelled",
                                        "detail": "conference setup was cancelled in the picker",
                                    }
                                )
                            ]
                    return [_text(await _handle_conference(conf_args))]
                if name == "configure_council":
                    return [_text(_handle_configure_council(arguments or {}))]
                if name == "list_models":
                    listing = await _handle_list_models(arguments or {})
                    provider = str((arguments or {}).get("provider") or "").strip().lower()
                    task = str((arguments or {}).get("task") or "").strip()
                    if (
                        provider in ("atlas", "openrouter")
                        and task
                        and bool((arguments or {}).get("interactive", True))
                    ):
                        # The picker is a convenience over the listing: if it fails,
                        # return the plain listing rather than lose it to an error.
                        try:
                            listing["selection"] = await _elicit_atlas_selection(
                                server, listing, task, provider=provider
                            )
                        except Exception:  # noqa: BLE001
                            listing["selection"] = {"supported": True, "action": "fallback"}
                    return [_text(listing)]
                if name == "unload_lms_model":
                    return [_text(await _handle_unload_lms(arguments or {}))]
                if name == "host_status":
                    return [_text(await _handle_host_status(arguments or {}))]
                if name == "diagnose":
                    return [_text(await _handle_diagnose(arguments or {}))]
                if name == "configure_tracing":
                    return [_text(_handle_configure_tracing(arguments or {}))]
                if name == "configure_disabled":
                    return [_text(_handle_configure_disabled(arguments or {}))]
                if name == "code_index":
                    return [_text(await asyncio.to_thread(_handle_code_index, arguments or {}))]
                if name == "code_search":
                    return [_text(await asyncio.to_thread(_handle_code_search, arguments or {}))]
                if name == "context_read":
                    return [_text(_handle_context_read(arguments or {}))]
                if name == "context":
                    return [_text(_handle_context(arguments or {}))]
                if name == "reset_session":
                    return [_text(await _handle_reset(store, arguments or {}))]
                if name == "stats":
                    return [_text(await _handle_stats(arguments or {}))]
                if name == "trace_list":
                    args = arguments or {}
                    return [
                        _text(
                            await asyncio.to_thread(
                                trace_query.list_traces,
                                audit.audit_path(),
                                limit=int(args.get("limit") or 20),
                                tool=str(args.get("tool") or "").strip() or None,
                                status=str(args.get("status") or "").strip() or None,
                                provider=str(args.get("provider") or "").strip() or None,
                                session=str(args.get("session") or "").strip() or None,
                                project=str(args.get("project") or "").strip() or None,
                                before=str(args.get("before") or "").strip() or None,
                            )
                        )
                    ]
                if name == "trace_get":
                    args = arguments or {}
                    return [
                        _text(
                            await asyncio.to_thread(
                                trace_query.get_trace,
                                audit.audit_path(),
                                str(args.get("trace_id") or "").strip(),
                                include_content=bool(args.get("include_content") or False),
                                max_chars=int(args.get("max_chars") or 4000),
                            )
                        )
                    ]
                if name == "session_list":
                    args = arguments or {}
                    return [
                        _text(
                            hub.list_sessions(
                                project=trace_runtime.project_fingerprint(),
                                all_projects=bool(args.get("all_projects") or False),
                                limit=int(args.get("limit") or 50),
                                active_only=(
                                    bool(args["active_only"]) if "active_only" in args else True
                                ),
                            )
                        )
                    ]
                if name == "session_peek":
                    args = arguments or {}
                    return [
                        _text(
                            hub.peek_session(
                                session_key=str(args.get("session_key") or "").strip(),
                                agent_id=(str(args.get("agent_id") or "").strip() or None),
                            )
                        )
                    ]
                if name == "session_stats":
                    args = arguments or {}
                    window_raw = args.get("window_s", hub._DEFAULT_STATS_WINDOW_S)
                    try:
                        window_s = int(window_raw)
                    except (TypeError, ValueError):
                        window_s = hub._DEFAULT_STATS_WINDOW_S
                    return [
                        _text(
                            hub.hub_stats(
                                project=trace_runtime.project_fingerprint(),
                                all_projects=bool(args.get("all_projects") or False),
                                window_s=window_s,
                            )
                        )
                    ]
                return [
                    _text(
                        {
                            "status": "error",
                            "kind": "unknown_tool",
                            "detail": f"unknown tool: {name}",
                        }
                    )
                ]
            except Exception as exc:  # noqa: BLE001 — never let an exception escape the turn
                trace_runtime.record_stage("validation", "error")
                return [
                    _text(
                        {
                            "status": "error",
                            "kind": "sdk_error",
                            "detail": f"{type(exc).__name__}: {exc}",
                        }
                    )
                ]

    return server


async def _handle_ask(store: SessionStore, args: dict) -> dict:
    """`ask` — the STATEFUL (multi-turn) tool, on Fable by default or Opus.

    ``oracle`` selects the multi-turn model. The whole Opus family names the one
    Opus session (the same rule `_handle_reset` uses), so ``oracle="opus5"`` and
    ``oracle="opus48"`` both resume the newest Opus — there is no per-pin
    namespace, and no new capability versus the old ``ask_opus5`` tool, which
    always resolved through the ladder. Anything that is not an Anthropic
    multi-turn model is refused with a pointer to the stateless `ask_model`."""
    raw = str(args.get("oracle") or "fable").strip().lower()
    oracle = oracles.ALIASES.get(raw, raw)
    if oracle.startswith("opus"):
        return await _handle_opus5(store, args)
    if oracle and oracle != "fable":
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": (
                f"`ask` is the multi-turn tool; `oracle` must be 'fable' or the Opus "
                f"family (got {raw!r}). For a single-turn model use "
                "`ask_model(provider=…)`."
            ),
        }
    return await _handle_claude_ask(
        store,
        args,
        tool="ask",
        bridge=fable,
        display="Fable",
        model=fable.fable_model(),
        oracle_key="fable",
    )


async def _handle_opus5(store: SessionStore, args: dict) -> dict:
    """The multi-turn Claude Opus path (newest available), reached from
    `ask(oracle="opus")` and from the legacy `ask_opus5` alias.

    Sessions are namespaced so the same key on Fable and Opus is two independent
    conversations (an SDK session id is bound to the model that created it, so
    resuming a Fable thread on Opus would silently swap models mid-conversation).
    `opus` is a ladder like `fable`, so `model` resolves to the newest Opus this
    build serves."""
    return await _handle_claude_ask(
        store,
        args,
        tool="ask_opus5",
        bridge=opus,
        display="Opus",
        model=opus.opus_model(),
        oracle_key="opus",
        session_prefix=OPUS_SESSION_NS,
    )


# Session-key namespace for `ask_opus5`, so its sessions can't collide with (or
# resume) `ask`'s Fable threads. `reset_session(model='opus5')` applies the same
# prefix, which is why it lives at module scope.
OPUS_SESSION_NS = "opus5:"


# WeakValueDictionary so a lock is evicted once nothing holds or awaits it — an
# uncontended session leaves no entry behind. A caller keeps a strong ref for the
# duration of its ``async with`` (and any waiter holds one too), so a lock can't be
# collected out from under concurrent users; only genuinely-idle ones drop, which
# bounds growth (a plain dict grew one entry per distinct slug forever).
_SESSION_LOCKS: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _session_lock(key: str) -> asyncio.Lock:
    """Per-session async lock. Two concurrent `ask`/`ask_opus5` calls on the SAME
    session must not both read the same resume id and fork the conversation (one
    turn's server-side context orphaned, ``sdk_session_id`` last-writer-wins). The
    event loop is single-threaded, so this get/create needs no lock of its own.
    Keyed by (running-loop id, NAMESPACED session key): the loop id keeps an
    ``asyncio.Lock`` from ever being reused across event loops (it would attach a
    Future to the wrong loop) — in production there is one loop forever, so it's a
    constant; the session namespace keeps ask vs ask_opus5 on the same slug (two
    independent conversations) from serializing against each other."""
    k = (id(asyncio.get_running_loop()), key)
    lock = _SESSION_LOCKS.get(k)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[k] = lock
    return lock


def _session_key(prefix: str, session: object) -> str:
    """The namespaced session key — the SAME derivation the ask pipeline and
    ``reset_session`` must both use, or ``_session_lock`` hands them different locks and the
    guard silently no-ops. ``prefix`` (``OPUS_SESSION_NS``) keeps ask_opus5's sessions apart
    from ask's on the same slug."""
    return prefix + str(session or "default")


def _session_label_error(prefix: str, session: object) -> dict | None:
    """``bad_args`` for an un-namespaced (Fable) label that starts with the Opus prefix.

    Fable keys carry no prefix, so a Fable session named ``opus5:review`` IS the Opus
    session ``review``'s key — one lock, one resume id, one reset, one hub row, and a
    Fable turn resuming an Opus conversation. Refused rather than giving Fable keys a
    prefix of their own, which would re-key every existing Fable session."""
    label = str(session or "")
    if prefix or not label.startswith(OPUS_SESSION_NS):
        return None
    return {
        "status": "error",
        "kind": "bad_args",
        "detail": (
            f"session labels starting with {OPUS_SESSION_NS!r} name Opus sessions; pick "
            f"another label for Fable, or ask Opus with oracle='opus' and "
            f"session={label[len(OPUS_SESSION_NS):]!r}"
        ),
    }


async def _handle_claude_ask(
    store: SessionStore,
    args: dict,
    *,
    tool: str,
    bridge,
    display: str,
    model: str,
    oracle_key: str,
    session_prefix: str = "",
) -> dict:
    """Serialize same-session turns, then run the shared multi-turn pipeline. The
    lock spans the whole call so the resume-id read, the model run, and the session
    record can't interleave with another turn on the same key (R1 fork race)."""
    # The multi-turn tools call the bridge directly (not oracles.run), so the
    # denylist gate that guards every other path must be applied here too — else a
    # disabled `fable`/`opus` still answers on `ask`/`ask_opus5`. Checked before the
    # lock: a refused call has no session work to serialize.
    if oracles.is_disabled(oracle_key):
        return {
            "status": "error",
            "kind": "disabled",
            "detail": (
                f"{display} is disabled by the operator ({oracles.DISABLED_KEY}); "
                "re-enable it with the configure_disabled tool or by editing the denylist"
            ),
            "model": model,
        }
    bad_label = _session_label_error(session_prefix, args.get("session"))
    if bad_label is not None:
        return bad_label
    key = _session_key(session_prefix, args.get("session"))
    async with _session_lock(key):
        return await _handle_claude_ask_locked(
            store,
            args,
            tool=tool,
            bridge=bridge,
            display=display,
            model=model,
            oracle_key=oracle_key,
            session_prefix=session_prefix,
        )


async def _handle_claude_ask_locked(
    store: SessionStore,
    args: dict,
    *,
    tool: str,
    bridge,
    display: str,
    model: str,
    oracle_key: str,
    session_prefix: str = "",
) -> dict:
    """Shared multi-turn Anthropic handler — the implementation behind `ask`
    (Fable) and `ask_opus5` (Claude Opus 5). Always entered under the per-session
    lock via :func:`_handle_claude_ask`.

    Both run the identical pipeline (reset → resolve refs → guard → run the
    bridge → refused/error/ok branches → sidecar → session record → save); only
    the tool name, bridge module, label, and session namespace differ.
    ``bridge`` is a module exposing ``run(question, context, *, resume, on_think)``
    — resolved at call time so tests can monkeypatch ``server.fable.run``."""
    question = str(args.get("question") or "")
    session = str(args.get("session") or "default")
    key = session_prefix + session  # store/audit/hub key; `session` is what callers see
    reset = bool(args.get("reset") or False)
    requested_trusted = bool(args.get("trusted") or False)
    trusted = requested_trusted and _trusted_allowed()

    rep = Reporter(f"{tool} · {display}")
    rep.info("question", _preview(question))
    if trusted:
        rep.info("trusted session", session)
    elif requested_trusted:
        rep.warn("trusted flag ignored — operator must set ASK_FABLE_ALLOW_TRUSTED to enable")

    dumped: str | None = None
    if reset:
        dumped = store.reset(key, save=True)
        rep.info("session reset", session)

    # Resolve refs AFTER reset so session-history counts only post-reset context.
    context, ref_resolved, ref_missing, ref_fail = _prepare_context(
        args, has_history=store.resume_id(key) is not None
    )
    _report_refs(rep, ref_resolved, ref_missing)
    if ref_fail is not None:  # all refs missing + no other context — don't call the model
        rep.fail("no context resolved (all context_ref keys missing)")
        rep.footer()
        return ref_fail

    allowed, reason = _guard_check(question, context, trusted=trusted)
    if not allowed:
        rep.fail(f"guard denied: {reason}")
        rep.footer()
        audit.record(
            decision="denied",
            stage="guard",
            reason=reason,
            question=question,
            context=context,
            session=key,
            model=model,
            trusted_session=trusted,
        )
        return _guard_refusal(reason)
    rep.ok("guard passed")

    rep.start(f"asking {display} ({model})")
    t0 = time.monotonic()
    sink = rep.stream_think(model) if _flag("ASK_FABLE_STREAM_REASONING") else None
    res = await bridge.run(
        question, context, resume=store.resume_id(key), **({"on_think": sink} if sink else {})
    )
    if oracles.placeholder_telemetry(res):
        # Not ``is None``: OracleResult stamps every error result with a
        # transport="error" stub carrying no model and 0 ms, which would bucket
        # under "?" and drag this oracle's latency down.
        res.telemetry = oracles.fallback_telemetry(
            key=oracle_key,
            requested_model=model,
            wall_duration_ms=(time.monotonic() - t0) * 1000,
            actual_model=res.model,
            returncode=res.returncode,
            thinking=res.thinking,
            transport="sdk",
        )
    trace_runtime.record_provider(
        res.telemetry,
        res.status,
        res.thinking,
        kind=res.kind,
        answer=res.text if res.status == "ok" else None,
    )
    # `model` so far is what we RESOLVED to ask for. The Fable ladder can demote
    # mid-call, so the id that actually answered may be a rung lower — take it
    # from the result, or the payload, audit row, hub turn and saved transcript
    # would all credit a model that never ran.
    model = res.model or model
    duration_ms = int((time.monotonic() - t0) * 1000)
    secs = duration_ms / 1000
    if sink is None:  # already streamed live otherwise
        rep.think(model, res.thinking)

    if res.status == "refused":
        rep.warn(f"{display} refused: {res.text}", secs)
        rep.footer()
        audit.record(
            decision="refused",
            stage="model",
            reason=res.text,
            question=question,
            context=context,
            session=key,
            model=model,
            duration_ms=duration_ms,
        )
        return _model_refused_payload(res)

    if res.status == "error":
        detail = res.text
        if res.meta.get("resume_failed"):
            # The stored resume id is dead (its conversation is gone), and keeping it
            # would fail every later turn on this session until a reset. Forget it
            # so the next ask starts a fresh conversation — and say so.
            store.forget_resume(key)
            detail += (
                f" — session {session!r} could not be resumed, so its stored conversation "
                "id was dropped; the next ask on it starts a fresh conversation"
            )
        rep.fail(f"{display} error ({res.kind}): {detail}", secs)
        rep.footer()
        audit.record(
            decision="error",
            stage=None,
            reason=res.kind,
            question=question,
            context=context,
            session=key,
            model=model,
            duration_ms=duration_ms,
            outcome_detail=detail,
        )
        # `model` so the failure buckets under the oracle that failed: without it
        # stats(by="model") drops every errored call — including a breaker-shed
        # one — as "?", which is precisely the backend the operator is looking for.
        return {"status": "error", "kind": res.kind, "detail": detail, "model": model}

    rep.ok(f"{display} answered", secs)
    rep.footer()
    prose, sc = sidecar.extract(res.text)
    # PR2 loop terminator: track consecutive 'needs_more_context' turns on this
    # session; past the cap, stop the agent's re-ask loop with context_exhausted.
    blocked = bool(sc and sc.get("recommendation") == "needs_more_context")
    streak = store.bump_blocked(key, blocked)
    exhausted = blocked and streak > max(0, _int_env("ASK_FABLE_MAX_NEEDS_CONTEXT", 2))
    store.record_turn(key, question, prose, res.session_id, thinking=res.thinking)
    _hub_mirror(
        session_key=key,
        question=question,
        answer=prose,
        oracle=model,
        status="ok",
        sdk_session_id=res.session_id,
        duration_ms=duration_ms,
    )
    audit.record(
        decision="allowed",
        stage=None,
        reason="context_exhausted" if exhausted else "ok",
        question=question,
        context=context,
        session=key,
        model=model,
        duration_ms=duration_ms,
        trusted_session=trusted,
    )
    saved = outputs.save(
        tool=tool,
        model=model,
        question=question,
        answer=prose,
        context=context,
        session=key,
        thinking=res.thinking,
    )
    payload: dict = {
        "status": "context_exhausted" if exhausted else "ok",
        "model": model,
        "answer": prose,
        "sidecar": sc,
        "missing_sidecar": sc is None,
        "session": session,
    }
    # H1: the bridge's meta (`partial` + `stop_reason` on an answer the output cap cut
    # off) reached `ask_model` but not `ask` — the primary tool returned half an answer
    # as a plain `ok`, and recorded it into the transcript as a complete turn.
    for meta_key, meta_value in (res.meta or {}).items():
        payload.setdefault(meta_key, meta_value)
    _add_refs(payload, ref_resolved, ref_missing)
    _add_thinking(payload, res.thinking)
    if exhausted:
        payload["detail"] = (
            f"the model still lacked context after {streak} consecutive tries — stop re-asking "
            "on this session; proceed with your own judgment, gather the code a different way, or "
            "reset and ask a more specific question"
        )
    else:
        fu = _followup(sc, context, session)
        if fu:
            payload["followup"] = fu
    if saved:
        payload["saved"] = saved
    if dumped:
        payload["reset_dump"] = dumped
    return payload


async def _handle_single(
    args: dict,
    *,
    tool: str,
    oracle_key: str,
    session: str,
    display: str,
    model: str,
    effort: str | None = None,
    cache_result: bool = True,
) -> dict:
    """Shared single-oracle handler — the unified implementation behind
    ``ask_m3`` / ``ask_gemini`` / ``ask_codex`` / ``ask_grok`` / ``ask_glm`` /
    ``ask_deepseek`` / ``ask_ollama`` / ``ask_atlas``.

    Each of those tools runs the same pipeline (resolve refs → guard → cache
    lookup → run oracle → refused/error/ok branches → sidecar → save → payload);
    only the tool name, oracle key, model label, and audit session differ.
    ``effort`` is atlas answer-budget / grok reasoning-effort; other oracles ignore it.
    For ``grok``, ``model`` is also passed through to the bridge (env default otherwise).
    ``cache_result=False`` skips both the lookup and the store (used by
    ``ask_websearch``, whose live-web answers are time-sensitive and non-idempotent)."""
    question = str(args.get("question") or "")

    rep = Reporter(f"{tool} · {display}")
    rep.info("question", _preview(question))

    trusted = _resolve_trusted(args)
    if trusted:
        rep.info("trusted session", session)

    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    _report_refs(rep, ref_resolved, ref_missing)
    if ref_fail is not None:
        rep.fail("no context resolved (all context_ref keys missing)")
        rep.footer()
        return ref_fail

    allowed, reason = _guard_check(question, context, trusted=trusted)
    if not allowed:
        rep.fail(f"guard denied: {reason}")
        rep.footer()
        audit.record(
            decision="denied",
            stage="guard",
            reason=reason,
            question=question,
            context=context,
            session=session,
            model=model,
            trusted_session=trusted,
        )
        return _guard_refusal(reason)
    rep.ok("guard passed")

    ck = None
    if cache_result:
        # M2: `effort=None` means "the operator's default", which lives in config/env
        # and changes between calls — and between processes, since cache.db outlives
        # both. The oracle layer resolves it, but this OUTER hit returns first, so a
        # grok/kimi/codex answer computed at the old default kept being served.
        ck, served = _cache_lookup(
            tool, [model], question, context, effort=oracles.cache_effort(oracle_key, effort)
        )
        if served is not None:
            rep.ok(f"cache hit ({served['cache_age_s']}s old)")
            rep.footer()
            return served

    rep.start(f"asking {display} ({model})")
    t0 = time.monotonic()
    # Named CLI bridges (grok) honor an explicit model override; dynamic tokens
    # (atlas:/ollama:) carry the model in the key itself. `websearch` carries the
    # caller's model selection (grok / a Claude key) through to the router.
    bridge_model = model if oracle_key in ("grok", "kimi", "websearch") else None
    res = await oracles.run(
        oracle_key,
        question,
        context,
        effort=effort,
        model=bridge_model,
    )
    duration_ms = int((time.monotonic() - t0) * 1000)
    secs = duration_ms / 1000
    rep.think(model, res.thinking)

    if res.status == "refused":
        rep.warn(f"{display} refused: {res.text}", secs)
        rep.footer()
        audit.record(
            decision="refused",
            stage="model",
            reason=res.text,
            question=question,
            context=context,
            session=session,
            model=model,
            duration_ms=duration_ms,
        )
        return _model_refused_payload(res)

    if res.status == "error":
        rep.fail(f"{display} error ({res.kind}): {res.text}", secs)
        rep.footer()
        audit.record(
            decision="error",
            stage=None,
            reason=res.kind,
            question=question,
            context=context,
            session=session,
            model=model,
            duration_ms=duration_ms,
            outcome_detail=res.text,
        )
        # `model` so the failure buckets under the oracle that failed: without it
        # stats(by="model") drops every errored call — including a breaker-shed
        # one — as "?", which is precisely the backend the operator is looking for.
        payload = {"status": "error", "kind": res.kind, "detail": res.text, "model": model}
        # Bridge-attached offers (e.g. LM Studio's `unload_offer`) must reach the
        # caller: that structured option is what the agent offers the operator.
        for k, v in (res.meta or {}).items():
            payload.setdefault(k, v)
        return payload

    rep.ok(f"{display} answered", secs)
    rep.footer()
    audit.record(
        decision="allowed",
        stage=None,
        reason="ok",
        question=question,
        context=context,
        session=session,
        model=model,
        duration_ms=duration_ms,
    )
    prose, sc = sidecar.extract(res.text)
    _hub_mirror(
        session_key=session,
        question=question,
        answer=prose,
        oracle=model,
        status="ok",
        duration_ms=duration_ms,
    )
    saved = outputs.save(
        tool=tool,
        model=res.model,
        question=question,
        answer=prose,
        context=context,
        thinking=res.thinking,
    )
    payload = {
        "status": "ok",
        "model": res.model,
        "answer": prose,
        "sidecar": sc,
        "missing_sidecar": sc is None,
    }
    # Bridge-specific facts (e.g. LM Studio's load/swap confirmation) ride
    # alongside without being able to overwrite the core payload keys.
    for k, v in (res.meta or {}).items():
        payload.setdefault(k, v)
    _add_refs(payload, ref_resolved, ref_missing)
    _add_thinking(payload, res.thinking)
    fu = _followup(sc, context)
    if fu:
        payload["followup"] = fu
    if saved:
        payload["saved"] = saved
    # A bridge-flagged truncation must not be pinned in the TOOL cache either —
    # the oracle-level cache already skips it (see oracles._run), and a cached
    # truncated answer would otherwise outlive the context fix that would make
    # the next call correct. ``cache_result=False`` (ask_websearch) never pins.
    if cache_result and ck is not None and res.kind != "truncated":
        cache.put(ck, trace_runtime.prepare_cache_store(payload))
    return payload


async def _handle_sonnet(args: dict) -> dict:
    """Ask Claude Sonnet 5 on its own — guarded, single-turn. Rides the same OAuth
    session as `ask`/`ask_opus5`, so it's always available (no key/CLI)."""
    return await _handle_single(
        args,
        tool="ask_sonnet",
        oracle_key="sonnet",
        session="sonnet",
        display="Sonnet 5",
        model=oracles.label("sonnet"),
    )


async def _handle_m3(args: dict) -> dict:
    """Ask MiniMax (MiniMax-M3) on its own — guarded, single-turn."""
    return await _handle_single(
        args,
        tool="ask_m3",
        oracle_key="minimax",
        session="m3",
        display="MiniMax",
        model=minimax.minimax_model(),
    )


async def _handle_gemini(args: dict) -> dict:
    """Ask Gemini (Gemini 3.1 Pro) on its own via the local `agy` CLI — guarded,
    single-turn. Reports binary_missing when the CLI isn't installed/logged in."""
    return await _handle_single(
        args,
        tool="ask_gemini",
        oracle_key="gemini",
        session="gemini",
        display="Gemini",
        model=gemini.gemini_model(),
    )


async def _handle_codex(args: dict) -> dict:
    """Ask OpenAI's model (GPT-5.6 Sol) on its own via the local `codex exec` CLI —
    guarded, single-turn. Reports binary_missing when the CLI isn't installed/logged in."""
    return await _handle_single(
        args,
        tool="ask_codex",
        oracle_key="codex",
        session="codex",
        display="Codex",
        model=codex.codex_model(),
    )


async def _handle_grok(args: dict) -> dict:
    """Ask xAI Grok (grok-4.6) on its own via the local `grok` CLI — guarded,
    single-turn. Prefer this over ``ask_atlas`` with ``xai/grok-*`` when the
    binary is installed. Reports binary_missing when the CLI isn't on PATH.
    ``model``/``effort`` override the CLI default."""
    return await _handle_single(
        args,
        tool="ask_grok",
        oracle_key="grok",
        session="grok",
        display="Grok",
        model=str(args.get("model") or "").strip() or grok.grok_model(),
        effort=str(args.get("effort") or "").strip().lower() or None,
    )


async def _handle_kimi(args: dict) -> dict:
    """Ask Moonshot Kimi (kimi-code/k3) on its own via the local `kimi` CLI —
    guarded, single-turn. Prefer this over ``ask_atlas`` with ``moonshotai/kimi-*``
    when the binary is installed: it runs on the operator's Kimi Code subscription
    rather than per-token Atlas billing, and k3 carries 1M context against Atlas's
    262k. Reports binary_missing when the CLI isn't on PATH. ``model``/``effort``
    override the CLI default."""
    return await _handle_single(
        args,
        tool="ask_kimi",
        oracle_key="kimi",
        session="kimi",
        display="Kimi",
        model=str(args.get("model") or "").strip() or oracles.label("kimi"),
        effort=str(args.get("effort") or "").strip().lower() or None,
    )


async def _handle_websearch(args: dict) -> dict:
    """Opt-in web-search / OSINT research agent — the ONE ask_* tool that browses.

    OFF by default: the operator must set ASK_FABLE_ALLOW_WEBSEARCH (env or config),
    mirroring how ``ASK_FABLE_ALLOW_RUN`` gates the sandbox. The tool stays in
    ``list_tools`` when disabled (the in-repo convention — no tool is ever hidden by
    a flag); it just returns ``status:"disabled"`` at call time. ``model`` selects
    the search-capable backend (grok or a Claude model); the router flips web search
    on and hands the model a research/OSINT prompt. Results are never cached (web
    facts are time-sensitive), so ``cache_result=False``."""
    if not _flag("ASK_FABLE_ALLOW_WEBSEARCH"):
        return {
            "status": "disabled",
            "reason": "ask_websearch is off (set ASK_FABLE_ALLOW_WEBSEARCH=1 to enable)",
            "hint": (
                "This is the only ask_* tool that reaches the live web, so it is "
                "opt-in. Enable it via the env var or the ask_fable config file."
            ),
        }
    model_key = websearch.resolve(args.get("model"))
    if model_key is None:
        allowed = _WEBSEARCH_SCHEMA["properties"]["model"]["enum"]
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": f"unknown websearch model {args.get('model')!r}; choose one of {allowed}",
        }
    # The router drives its backend's bridge directly, beneath the denylist gate in
    # oracles.run (which only ever sees the "websearch" key), so honor the operator's
    # denylist for the backend here. Its `opus5` is the Opus LADDER — the model
    # `ask(oracle="opus")` serves — so disabling `opus` turns it off too.
    backend = "opus" if model_key == "opus5" else model_key
    if oracles.is_disabled(model_key) or oracles.is_disabled(backend):
        return {
            "status": "error",
            "kind": "disabled",
            "detail": (
                f"ask_websearch backend {model_key!r} is disabled by the operator "
                f"({oracles.DISABLED_KEY}); pick another `model`, or re-enable it with "
                "the configure_disabled tool"
            ),
            "model": model_key,
        }
    return await _handle_single(
        args,
        tool="ask_websearch",
        oracle_key="websearch",
        session="websearch",
        display="Web Search",
        model=model_key,
        cache_result=False,
    )


async def _handle_lms(args: dict) -> dict:
    """Ask a single model on an LM Studio server — guarded, single-turn.

    The model is loaded explicitly if it is not already resident (an explicit
    load never triggers Auto-Evict, so a resident model is not bumped off). If
    it genuinely does not fit alongside a resident model, the swap path unloads
    the resident one only when ``lmstudio_swap=auto`` (default), waiting until
    each unload/load is confirmed; the answer payload reports what was loaded,
    swapped and the context window in use."""
    model = str(args.get("model") or "").strip() or lmstudio.default_model()
    if not model:
        model = await asyncio.to_thread(lmstudio.loaded_default)
    if not model:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": "no model given, none configured (lmstudio_model / "
            "ASK_FABLE_LMSTUDIO_MODEL) and no single resident model — call "
            "`list_lms_models` and pass one",
        }
    return await _handle_single(
        args,
        tool="ask_lms",
        oracle_key=oracles.LMS_PREFIX + model,
        session="lmstudio",
        display="LM Studio",
        model=model,
    )


async def _handle_glm(args: dict) -> dict:
    """Ask GLM (GLM-5.2) on its own via its Anthropic-compatible endpoint — guarded,
    single-turn. Needs ASK_FABLE_GLM_API_KEY; reports not_configured otherwise."""
    return await _handle_single(
        args,
        tool="ask_glm",
        oracle_key="glm",
        session="glm",
        display="GLM",
        model=oracles.label("glm"),
    )


async def _handle_deepseek(args: dict) -> dict:
    """Ask DeepSeek (deepseek-v4-pro) on its own via its Anthropic-compatible endpoint —
    guarded, single-turn. Needs ASK_FABLE_DEEPSEEK_API_KEY; reports not_configured otherwise."""
    return await _handle_single(
        args,
        tool="ask_deepseek",
        oracle_key="deepseek",
        session="deepseek",
        display="DeepSeek",
        model=oracles.label("deepseek"),
    )


async def _handle_ollama(args: dict) -> dict:
    """Ask a single Ollama Cloud model on its own — guarded, single-turn."""
    model = str(args.get("model") or "").strip() or ollama.default_model()
    return await _handle_single(
        args,
        tool="ask_ollama",
        oracle_key=oracles.OLLAMA_PREFIX + model,
        session="ollama",
        display="Ollama",
        model=model,
    )


def _local_cli_can_carry(bridge, args: dict, *, gateway_ready: bool) -> bool:
    """May a single-model gateway Grok/Kimi call be served by the operator's local CLI?

    The same rule ``oracles._via_local_cli`` applies to council/chain tokens: always
    when the gateway is not configured (the local answer, or its error naming the fix,
    is all there is), otherwise only while the prompt fits the CLI's one argv value —
    refusing a big prompt locally with advice to "use the gateway" would route the
    retry straight back here. A ``context_ref`` that fails to resolve is left to
    ``_handle_single`` to report, exactly as before."""
    if not gateway_ready:
        return True
    context, _resolved, _missing, fail = _prepare_context(args)
    if fail is not None:
        return True
    return bridge.prompt_fits(str(args.get("question") or ""), context)


async def _handle_atlas(args: dict) -> dict:
    """Ask a single Atlas Cloud text model on its own — guarded, single-turn.
    The ``effort`` preset (quick/standard/deep) sets the answer budget; default
    is ``deep`` (max reasoning). Model defaults to the configured default
    (``atlas_model`` / ``ASK_FABLE_ATLAS_MODEL`` / built-in).

    When the requested model is an xAI Grok id and the local ``grok`` CLI is on
    PATH, this routes through the local bridge instead of Atlas (prefer local)."""
    model = str(args.get("model") or "").strip() or atlas.default_model()
    # Prefer maximum effort when the caller omits it (override with quick/standard).
    effort = str(args.get("effort") or "").strip().lower() or atlas.default_effort()
    if (
        grok.looks_like_grok_model(model)
        and grok.available()
        and _local_cli_can_carry(grok, args, gateway_ready=atlas.configured())
    ):
        # Prefer the authenticated local CLI over Atlas for Grok family models.
        local_model = model.split("/", 1)[-1] if "/" in model else model
        return await _handle_single(
            args,
            tool="ask_grok",
            oracle_key="grok",
            session="grok",
            display="Grok",
            model=local_model,
            effort=effort,
        )
    return await _handle_single(
        args,
        tool="ask_atlas",
        oracle_key=oracles.ATLAS_PREFIX + model,
        session="atlas",
        display="Atlas",
        model=model,
        effort=effort,
    )


async def _handle_ali(args: dict) -> dict:
    """Ask a single Alibaba Cloud (Qwen) reasoning model on its own — guarded,
    single-turn, over the gateway's Anthropic Messages surface (thinking captured).
    Model defaults to `ali.default_model()` (qwen3.8-max)."""
    model = str(args.get("model") or "").strip() or ali.default_model()
    return await _handle_single(
        args,
        tool="ask_ali",
        oracle_key=oracles.ALI_PREFIX + model,
        session="ali",
        display="Alibaba",
        model=model,
    )


async def _handle_list_ali(args: dict) -> dict:
    """List the live Alibaba/Qwen reasoning-model catalog as a ready-to-render menu.

    Fetched from the gateway's OpenAI-compatible catalog endpoint (the Anthropic app
    has no list). ``all=true`` includes the non-reasoning audio/image models too."""
    if not bool(args.get("refresh", True)):
        return {"cloud_ok": False, "models": [], "hint": "refresh=false; no network call made"}
    if not ali.configured():
        return {
            "status": "error",
            "kind": "not_configured",
            "detail": "ali not configured (set ASK_FABLE_ALI_API_KEY)",
        }
    rep = Reporter("list_ali_models · Alibaba")
    rep.info("fetching catalog")
    t0 = time.monotonic()
    cat = await asyncio.to_thread(ali.catalog)
    secs = time.monotonic() - t0
    models = cat.get("all") if bool(args.get("all", False)) else cat.get("models")
    if cat.get("cloud_ok"):
        rep.ok(f"{len(cat.get('models') or [])} reasoning models", secs)
    else:
        rep.warn(f"catalog unreachable: {cat.get('error', 'unknown')}", secs)
    rep.footer()
    audit.record(
        decision="allowed",
        stage=None,
        reason="catalog",
        question="",
        context="",
        session="ali",
        model=None,
        duration_ms=int(secs * 1000),
    )
    return {
        "cloud_ok": cat.get("cloud_ok", False),
        "models": models or [],
        "default": ali.default_model(),
        "error": cat.get("error"),
        "hint": "invoke ask_ali with `model` set to one of these ids",
    }


async def _handle_openrouter_council(args: dict) -> dict:
    """Fan a question out to several OpenRouter models, then synthesize the answers.

    The sibling of ``ask_atlas_council``. ``models`` is a list of OpenRouter ids
    (an 'openrouter:' prefix is optional); defaults to the configured set
    (``configure_openrouter_council`` / ASK_FABLE_OPENROUTER_COUNCIL), else 3
    featured catalog models, one per provider. The adjudicator defaults GPT-first:
    the local `codex` CLI when installed, else OpenRouter-hosted gpt-5.6-sol,
    else Fable.
    """
    question = str(args.get("question") or "")
    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    if ref_fail is not None:
        return ref_fail
    raw = args.get("models") or openrouter.council_models()  # explicit → configured set
    if not raw:  # nothing configured — derive a small panel from the live catalog
        cat = await asyncio.to_thread(openrouter.catalog)
        if not cat.get("cloud_ok"):
            return {
                "status": "error",
                "kind": "no_models",
                "detail": "no openrouter models given, none configured, and the OpenRouter "
                "catalog is unreachable — pass `models` or run configure_openrouter_council",
            }
        raw = [it["model_id"] for it in cat.get("featured", [])[:3]]
    # Preserve id casing (gateway ids are case-sensitive); prefix matching and the
    # de-dupe are case-insensitive, first-seen spelling wins.
    selected: list[str] = []
    seen: set[str] = set()
    for m in raw:
        tok = str(m).strip()
        if not tok:
            continue
        if tok.lower().startswith(oracles.OPENROUTER_PREFIX):
            tok = oracles.OPENROUTER_PREFIX + tok[len(oracles.OPENROUTER_PREFIX) :]
        else:
            tok = oracles.OPENROUTER_PREFIX + tok
        if oracles.openrouter_model(tok) and tok.lower() not in seen:
            seen.add(tok.lower())
            selected.append(tok)
    if not selected:
        return {"status": "error", "kind": "no_models", "detail": "no openrouter models given"}
    if not openrouter.configured():
        # Grok/Kimi members reroute to the operator's local CLIs keylessly;
        # anything else needs the OpenRouter chat endpoint.
        reroutable = all(
            (grok.available() and grok.looks_like_grok_model(oracles.openrouter_model(t) or ""))
            or (
                kimi.available()
                and kimi.local_alias_for(oracles.openrouter_model(t) or "") is not None
            )
            for t in selected
        )
        if not reroutable:
            return {
                "status": "error",
                "kind": "not_configured",
                "detail": "openrouter not configured (set ASK_FABLE_OPENROUTER_API_KEY, or "
                "OPENROUTER_API_KEY which other OpenRouter tooling already uses); only "
                "Grok and Kimi members can run without it via the local CLIs",
            }
    explicit = str(args.get("synthesizer") or "").strip()
    synth_raw = explicit or openrouter.synthesizer_token() or ""
    if synth_raw:
        synth_tok, synth_err = _resolve_synth_token(synth_raw, oracles.OPENROUTER_PREFIX)
        if synth_err is not None:
            return {"status": "error", "kind": "bad_args", "detail": synth_err}
        note = "explicit synthesizer" if explicit else "persisted openrouter_synthesizer config"
    elif oracles.available("codex"):
        synth_tok = "codex"
        note = "local codex CLI (GPT-5.6 Sol) preferred over the hosted model"
    elif openrouter.configured():
        synth_tok = oracles.OPENROUTER_PREFIX + openrouter.DEFAULT_OPENROUTER_SYNTH_MODEL
        note = "codex CLI not installed; using OpenRouter-hosted GPT-5.6 Sol"
    else:
        synth_tok, note = "fable", "codex CLI not installed and OpenRouter unconfigured"
    session = str(args.get("session") or "").strip() or None
    trace_runtime.set_orchestration(
        mode="council", models=list(selected), flavor="openrouter", synthesizer=synth_tok
    )
    return await _council(
        question,
        context,
        selected,
        [],
        "ask_openrouter_council",
        ref_resolved,
        ref_missing,
        session=session,
        synthesizer=synth_tok,
        synth_note=note,
        trusted=_resolve_trusted(args),
    )


def _handle_configure_openrouter(args: dict) -> dict:
    """Persist the user's OpenRouter council (and optional synthesizer) to the
    config file so it survives across sessions and overrides the env defaults."""
    rep = Reporter("configure_openrouter_council")
    patch: dict = {}

    if "models" in args and args.get("models") is not None:
        raw = args.get("models") or []
        if not isinstance(raw, list):
            return {"status": "error", "kind": "bad_args", "detail": "`models` must be a list"}
        models = openrouter.dedupe_models([str(m) for m in raw if str(m).strip()])
        if not models:
            return {"status": "error", "kind": "bad_args", "detail": "`models` list is empty"}
        patch["openrouter_council"] = models

    if args.get("synthesizer"):
        tok, err = _resolve_synth_token(str(args["synthesizer"]), oracles.OPENROUTER_PREFIX)
        if err is not None:
            return {"status": "error", "kind": "bad_args", "detail": err}
        patch["openrouter_synthesizer"] = tok

    if not patch:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": "nothing to configure — pass `models` and/or `synthesizer`",
        }

    saved = config.save(patch)
    if saved is None:
        rep.fail("config write failed")
        rep.footer()
        return {"status": "error", "kind": "write_failed", "detail": "could not write config file"}

    council = patch.get("openrouter_council")
    rep.ok("saved council: " + ", ".join(council) if council else "config updated")
    rep.footer()
    return {
        "status": "ok",
        "saved_to": saved,
        "openrouter_council": openrouter.council_models(),
        "synthesizer": openrouter.synthesizer_token(),
    }


async def _handle_openrouter(args: dict) -> dict:
    """Ask a single OpenRouter model on its own — guarded, single-turn.

    Mirrors `ask_atlas`, including the prefer-local rule: a Grok or Kimi id the
    operator can already serve from an authenticated CLI is routed there rather
    than billed per token by the gateway."""
    model = str(args.get("model") or "").strip() or openrouter.default_model()
    effort = str(args.get("effort") or "").strip().lower() or openrouter.default_effort()
    gateway_ready = openrouter.configured()
    if (
        grok.looks_like_grok_model(model)
        and grok.available()
        and _local_cli_can_carry(grok, args, gateway_ready=gateway_ready)
    ):
        local_model = model.split("/", 1)[-1] if "/" in model else model
        return await _handle_single(
            args,
            tool="ask_grok",
            oracle_key="grok",
            session="grok",
            display="Grok",
            model=local_model,
            effort=effort,
        )
    if (
        kimi.available()
        and kimi.local_alias_for(model) is not None
        and _local_cli_can_carry(kimi, args, gateway_ready=gateway_ready)
    ):
        return await _handle_single(
            args,
            tool="ask_kimi",
            oracle_key="kimi",
            session="kimi",
            display="Kimi",
            model=model,
            effort=effort,
        )
    return await _handle_single(
        args,
        tool="ask_openrouter",
        oracle_key=oracles.OPENROUTER_PREFIX + model,
        session="openrouter",
        display="OpenRouter",
        model=model,
        effort=effort,
    )


# The consolidated single-model dispatch: `provider` selects the backend and the
# handler below is the existing per-backend wrapper, so every internal label
# (audit `tool`, hub `session`, cache key, output filename) is preserved exactly.
# A gateway provider needs a `model` (or falls back to its configured default); a
# fixed-model provider rejects one rather than silently ignoring it.
_MODEL_PROVIDER_HANDLERS = {
    "sonnet": _handle_sonnet,
    "minimax": _handle_m3,
    "glm": _handle_glm,
    "deepseek": _handle_deepseek,
    "gemini": _handle_gemini,
    "codex": _handle_codex,
    "grok": _handle_grok,
    "kimi": _handle_kimi,
    "ollama": _handle_ollama,
    "lmstudio": _handle_lms,
    "atlas": _handle_atlas,
    "ali": _handle_ali,
    "openrouter": _handle_openrouter,
}
# Providers whose model is baked into the backend — a `model` argument is an
# error, not a silent no-op (the old ask_glm/ask_m3/… tools had no `model` prop,
# so this is a new footgun the consolidated schema could otherwise introduce).
_MODEL_FIXED_PROVIDERS = frozenset({"sonnet", "minimax", "glm", "deepseek", "gemini", "codex"})
# The multi-turn (stateful) oracles live on `ask` / `ask_opus5`, not `ask_model`.
_MULTITURN_PROVIDERS = frozenset(
    {"fable", "fable51", "opus", "opus55", "opus5", "opus48"}
)


def _canon_provider(token: str) -> str:
    """Canonicalize a provider spelling: lowercase, then fold aliases
    (m3→minimax, gpt→codex, xai→grok, claude-sonnet-5→sonnet, …)."""
    t = str(token or "").strip().lower()
    return oracles.ALIASES.get(t, t)


async def _handle_model(args: dict) -> dict:
    """`ask_model` — one stateless oracle, selected by `provider` (+ optional
    `model`). Routes to the same per-backend wrapper each dedicated tool used, so
    nothing about a call changes except that its backend is now an argument."""
    raw = str(args.get("provider") or "").strip()
    provider = _canon_provider(raw)
    if provider in _MULTITURN_PROVIDERS:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": (
                f"provider {raw!r} is multi-turn — use `ask` (Fable) or `ask_opus5` "
                "(Opus), which carry a `session` and keep context across calls"
            ),
        }
    handler = _MODEL_PROVIDER_HANDLERS.get(provider)
    if handler is None:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": (
                f"unknown provider {raw!r}; choose one of "
                f"{', '.join(_MODEL_PROVIDER_HANDLERS)} "
                "(aliases: m3=minimax, gpt=codex, xai=grok)"
            ),
        }
    if str(args.get("model") or "").strip() and provider in _MODEL_FIXED_PROVIDERS:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": (
                f"provider {provider!r} has a fixed model and accepts no `model` "
                "argument — drop it, or use a gateway/CLI provider (grok, kimi, "
                "ollama, lmstudio, atlas, ali, openrouter)"
            ),
        }
    return await handler({**args, "provider": provider})


async def _handle_list_openrouter(args: dict) -> dict:
    """The live OpenRouter catalog as a pickable menu. Free — no API key needed."""
    if not bool(args.get("refresh", True)):
        return {
            "status": "ok",
            "cloud_ok": False,
            "effort_choices": openrouter.effort_menu(),
            "default_model": openrouter.default_model(),
            "hint": "refresh=false — no catalog fetched",
        }
    task = str(args.get("task") or "").strip()
    try:
        limit = int(args.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(2, min(limit, 8))
    cat = await asyncio.to_thread(openrouter.catalog, 8.0, task=task, recommendation_limit=limit)
    out = {
        "status": "ok",
        "cloud_ok": cat["cloud_ok"],
        "configured": openrouter.configured(),
        "default_model": openrouter.default_model(),
        "effort_choices": cat["effort_choices"],
        "featured": cat["featured"],
        "model_count": len(cat["menu"]),
        "models": cat["models"][:200],
    }
    if task:
        out["task"] = task
        out["recommendations"] = cat["featured"]
    if cat.get("hint"):
        out["hint"] = cat["hint"]
    if not openrouter.configured():
        out["note"] = (
            "catalog is free, but asking a model needs ASK_FABLE_OPENROUTER_API_KEY "
            "(or OPENROUTER_API_KEY)"
        )
    return out


async def _elicit_atlas_selection(
    server: Server, listing: dict, task: str, *, provider: str = "atlas"
) -> dict:
    """Native model + effort picker over a task-ranked Atlas or OpenRouter listing;
    ``provider`` sets the wording and the effort default."""
    gateway, display = (openrouter, "OpenRouter") if provider == "openrouter" else (atlas, "Atlas")
    recommendations = listing.get("recommendations") or []
    if not recommendations:
        return {"supported": False, "action": "fallback"}
    try:
        session = server.request_context.session
        capabilities = session.client_params.capabilities
        elicitation = capabilities.elicitation
    except (LookupError, AttributeError):
        return {"supported": False, "action": "fallback"}
    if elicitation is None:
        return {"supported": False, "action": "fallback"}
    if getattr(elicitation, "form", None) is None:
        return {"supported": False, "action": "fallback"}

    model_ids = [item["model_id"] for item in recommendations]
    effort_choices = listing.get("effort_choices") or gateway.effort_menu()
    effort_ids = [item["value"] for item in effort_choices]
    schema = {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "title": f"{display} model",
                "description": "Task-ranked choices; cost and fit are shown in each label.",
                "enum": model_ids,
                # Only Atlas ranks a `picker_description`; an OpenRouter item has
                # just its `cost_note`.
                "enumNames": [
                    " — ".join(
                        part
                        for part in (
                            item["label"],
                            item.get("picker_description") or item.get("cost_note"),
                        )
                        if part
                    )
                    for item in recommendations
                ],
                "default": model_ids[0],
            },
            "effort": {
                "type": "string",
                "title": "Reasoning effort",
                "enum": effort_ids,
                "enumNames": [item["label"] for item in effort_choices],
                "default": gateway.default_effort(),
            },
        },
        "required": ["model", "effort"],
    }
    try:
        response = await session.elicit_form(
            f"Choose an {display} model and effort for: {task}",
            schema,
        )
    except McpError:
        return {"supported": True, "action": "fallback"}
    content = response.content or {}
    action = str(response.action)
    model = content.get("model")
    effort = content.get("effort")
    if action != "accept":
        return {"supported": True, "action": action}
    if model not in model_ids or effort not in effort_ids:
        return {"supported": True, "action": "fallback"}
    return {
        "supported": True,
        "action": "accept",
        "model": model,
        "effort": effort,
    }


async def _handle_list_atlas(args: dict) -> dict:
    """List the live Atlas Cloud text-model catalog as a ready-to-render menu.

    Returns ``featured`` (a ~8-model curated shortlist), the full ``menu`` (each
    item with model_id, label, cost_note, provider, tags, context, latency), and
    ``effort_choices`` — so the agent can render BOTH the model picker and the
    effort picker from one call, then invoke ``ask_atlas`` with the choice."""
    refresh = bool(args.get("refresh", True))
    if not refresh:
        return {
            "cloud_ok": False,
            "featured": [],
            "menu": [],
            "effort_choices": atlas.effort_menu(),
            "models": [],
            "balance_note": None,
            "hint": "refresh=false; no network call made",
        }
    rep = Reporter("list_atlas_models · Atlas")
    rep.info("fetching catalog")
    t0 = time.monotonic()
    task = str(args.get("task") or "").strip()
    limit = int(args.get("limit", 5))
    # A blocking urllib fetch (6 s timeout) — off the event loop, like every sibling lister.
    cat = await asyncio.to_thread(atlas.catalog, task=task, recommendation_limit=limit)
    secs = time.monotonic() - t0
    if cat.get("cloud_ok"):
        rep.ok(
            f"{len(cat.get('models') or [])} text models, {len(cat.get('featured') or [])} featured",
            secs,
        )
    else:
        rep.warn(f"catalog unreachable: {cat.get('hint', 'unknown')}", secs)
    rep.footer()
    audit.record(
        decision="allowed",
        stage=None,
        reason="catalog",
        question="",
        context="",
        session="atlas",
        model=None,
        duration_ms=int(secs * 1000),
    )
    return cat


async def _timed(awaitable):
    """Await ``awaitable`` and return (result, elapsed_seconds)."""
    t = time.monotonic()
    res = await awaitable
    return res, time.monotonic() - t


def is_partial(res) -> bool:
    """True when a result is an answer the provider CUT OFF at its output cap.

    ``oracle_common.shape_stopped`` returns those as ``status="ok"`` with
    ``kind="truncated"``, so every orchestrator that filtered on ``status`` alone
    counted half an answer as a whole one — it reached quorum, was synthesized as
    if complete, and the merged result was frozen in the cache for the TTL."""
    return getattr(res, "kind", "") == "truncated"


def _partial_models(results) -> list[str]:
    """Models whose answer was cut off, for the payload's ``partial`` field."""
    return [r.model for r in results if r.status == "ok" and is_partial(r)]


def _source(res) -> dict:
    """Public per-oracle record for the council payload (no reasoning trace).
    Panelists now emit a sidecar (shared oracle prompt); strip it so raw JSON blocks
    don't leak into the merged answer or the ``sources`` view, but surface each
    panelist's ``recommendation`` as attribution data (who endorsed what)."""
    d: dict = {"status": res.status, "model": res.model}
    if res.status == "ok":
        prose, sc = sidecar.extract(res.text)
        d["answer"] = prose
        # A cut-off answer must SAY so here: `status: ok` alone read as complete.
        if is_partial(res):
            d["partial"] = True
            d["kind"] = res.kind
        if sc and sc.get("recommendation"):
            d["recommendation"] = sc["recommendation"]
    elif res.status == "refused":
        d["reason"] = res.text
        # A provider-safeguard refusal is distinguishable from a plain model
        # refusal in the sources, so a panelist that was blocked upstream doesn't
        # read as one that declined on scope.
        if getattr(res, "kind", "") == "provider_refusal":
            d["kind"] = res.kind
    else:
        d["kind"], d["detail"] = res.kind, res.text
        # A structured offer (e.g. LM Studio's unload_offer) must survive into
        # the council sources so the agent can act on it, not just read prose.
        offer = (res.meta or {}).get("unload_offer")
        if offer:
            d["unload_offer"] = offer
    return d


def _dump_sources(results) -> list[dict]:
    """Per-oracle records for the on-disk dump — like ``_source`` but keeps each
    panelist's reasoning trace, which the public council payload deliberately omits
    (it would bloat the tool result the agent reads). Only the dump wants it."""
    out = []
    for res in results:
        d = _source(res)
        if res.thinking:
            d["thinking"] = res.thinking
        out.append(d)
    return out


# Recommendations that push toward ACTING vs opposing the action. Any cross-set pair
# is a material disagreement — "apply" enables the change while "reject"/"investigate"
# oppose or gate it. (Old code only caught apply∧reject, so "ship it" vs "look deeper
# first" read as a mere 'partial'.) NOTE: ``needs_more_context`` is deliberately NOT
# here — it's an abstention about the panelist's *inputs* ("the pasted context was too
# thin to judge"), not opposition to the action; pairing it with "apply" would force
# adjudication on the common case of one thin-context oracle, crying wolf. It still
# prevents 'strong' (via the coverage check) and shows up in the vote vector.
_ACT_RECS = frozenset({"apply"})
_BLOCK_RECS = frozenset({"reject", "investigate"})


def _consensus(
    recs: list[str],
    panel_size: int | None = None,
    confs: list[str] | None = None,
    requested: int | None = None,
) -> tuple[str, bool]:
    """Qualitative agreement over panelists' sidecar recommendations (NOT embeddings
    — those are polarity-blind for code). Returns (consensus, material_disagreement).

    Coverage-aware: ``panel_size`` is how many panelists ANSWERED; ``recs`` is only
    those that also emitted a usable recommendation. Unanimity among a *subset* of the
    panel is NOT 'strong' — a missing/malformed/multi-block sidecar silently drops a
    voter, and reporting 'strong' off two of five voices was the worst false-confidence
    mode. ``panel_size`` defaults to ``len(recs)`` (full coverage) when unset.

    ``confs`` (optional, parallel to ``recs``) downgrades a unanimous verdict to
    'partial' when every voter hedged 'low' — N models guessing in the same direction
    is not strong agreement.

    ``requested`` (optional) is how many panelists were ASKED — it gates 'strong',
    which requires EVERY requested model to have answered AND agreed. It defaults to
    ``panel_size`` (answered count) for standalone calls; the council passes the full
    requested size so that a 2-of-5 unanimous survivors panel reports 'partial', not
    'strong' — a caller keys on the ``consensus`` field, and 'strong' off a degraded
    panel was a false-confidence bug. ``panel_size`` (answered count) still separates
    'unknown' (<2 answered) from 'partial' (>=2 answered, incomplete).

    ``material_disagreement`` = at least one voter pushes to act (``apply``) while
    another opposes/gates it (``reject``/``investigate``). ``consensus``: 'strong' (all
    REQUESTED voters agreed, not all-low), 'divergent' (materially opposed),
    'partial' (mixed, incomplete-coverage, or hedged-low), 'unknown' (fewer than two
    panelists answered at all)."""
    if panel_size is None:
        panel_size = len(recs)
    if requested is None:
        requested = panel_size
    distinct = set(recs)
    material = bool(distinct & _ACT_RECS) and bool(distinct & _BLOCK_RECS)
    if material:
        return "divergent", True
    if len(recs) < 2:
        # <2 usable recommendations. A coverage gap (≥2 answered but a sidecar dropped
        # a voter) is 'partial'; only a genuinely thin panel (<2 answered) is 'unknown'.
        return ("partial", False) if (panel_size >= 2 and recs) else ("unknown", False)
    if len(distinct) == 1 and len(recs) == requested:
        present = [c for c in (confs or []) if c]
        if present and len(present) == len(recs) and all(c == "low" for c in present):
            return "partial", False  # unanimous but every voter hedged low
        return "strong", False
    return "partial", False  # unanimous-but-incomplete, or mixed-but-unopposed


def _vote_vector(recs: list[str], panel_size: int) -> dict[str, int]:
    """Per-recommendation tally plus a ``no_sidecar`` bucket for panelists that
    answered but gave no usable recommendation — so the caller can see a 'strong'/
    'partial' label rests on, e.g., 2 of a 5-oracle panel, not hide it."""
    votes: dict[str, int] = {}
    for r in recs:
        votes[r] = votes.get(r, 0) + 1
    missing = panel_size - len(recs)
    if missing > 0:
        votes["no_sidecar"] = missing
    return votes


def _group_slot_detail(role: str, raw: str) -> str | None:
    """Error detail for a group token ('twin') offered to a SINGLE-model slot, or
    None when ``raw`` names no group.

    ``oracles.resolve_ordered`` happily expands a group anywhere, and every
    single-model slot reads ``recognized[0]`` — so without this check
    ``synthesizer='twin'`` would quietly become plain Fable and the operator would
    never learn their second model was dropped."""
    members = oracles.group_members(raw)
    if members is None:
        return None
    return (
        f"{raw!r} names a model group ({' + '.join(members)}), and {role} is a single "
        f"model — name one of its members here, or use the group where a list of models "
        f"is taken (`models` on ask_council, `pipeline` on ask_chain)."
    )


def _resolve_synth_token(
    raw: str, bare_prefix: str = oracles.ATLAS_PREFIX
) -> tuple[str | None, str | None]:
    """Resolve a user-supplied synthesizer name to an oracle token.

    Accepts everything a council member list accepts — KNOWN names, aliases
    ('gpt' → codex), 'ollama:<model>' and 'atlas:<model-id>' tokens — plus a bare
    gateway model id like 'openai/gpt-5.6-sol' (an id with a '/' is retried with
    ``bare_prefix``: 'atlas:' by default, 'openrouter:' from the OpenRouter council,
    whose bare ids are documented as OpenRouter ids). Id casing is preserved
    (gateway ids are case-sensitive).
    A GROUP token is refused: the synthesizer is one model, not a panel.
    Returns (token, None), or (None, None) for empty input, or
    (None, error_detail) for an unrecognized name."""
    tok = (raw or "").strip()
    if not tok:
        return None, None
    group_detail = _group_slot_detail("a synthesizer", tok)
    if group_detail is not None:
        return None, group_detail
    recognized, _ = oracles.resolve_ordered([tok])
    if recognized:
        return recognized[0], None
    if "/" in tok and not tok.lower().startswith(bare_prefix):
        recognized, _ = oracles.resolve_ordered([bare_prefix + tok])
        if recognized:
            return recognized[0], None
    return None, (
        f"unknown synthesizer: {raw!r} — use one of {', '.join(oracles.KNOWN)}, an alias "
        f"({', '.join(sorted(oracles.ALIASES))}), or an "
        "'ollama:<model>'/'lmstudio:<model>'/'atlas:<model-id>' token"
    )


async def _elicit_conference_setup(server: Server, args: dict) -> dict:
    """Pop a native model+topic picker for a conference when the client supports
    form elicitation. Returns {"action":"accept","topic","models","rounds"} on
    accept, else {"action": "fallback"|"decline"|"cancel"} — the caller then runs
    with whatever args it already had (or cancels)."""
    try:
        session = server.request_context.session
        elicitation = session.client_params.capabilities.elicitation
    except (LookupError, AttributeError):
        return {"action": "fallback"}
    if elicitation is None or getattr(elicitation, "form", None) is None:
        return {"action": "fallback"}

    properties: dict = {
        "topic": {
            "type": "string",
            "title": "Topic",
            "description": "The question the conference should argue.",
            "default": str(args.get("question") or ""),
        }
    }
    for key in conference.CANDIDATES:
        properties[key] = {
            "type": "boolean",
            "title": oracles.label(key),
            "description": "available" if oracles.available(key) else "no key/CLI configured",
            "default": bool(oracles.available(key)),
        }
    properties["rounds"] = {
        "type": "integer",
        "title": "Rounds",
        "minimum": 1,
        "maximum": 10,
        "default": int(args.get("rounds") or 3),
    }
    schema = {"type": "object", "properties": properties, "required": ["topic"]}
    try:
        response = await session.elicit_form("Set up a brainstorm conference", schema)
    except McpError:
        return {"action": "fallback"}
    action = str(response.action)
    if action != "accept":
        return {"action": action}
    content = response.content or {}
    chosen = [k for k in conference.CANDIDATES if content.get(k)]
    return {
        "action": "accept",
        "topic": str(content.get("topic") or "").strip(),
        "models": chosen,
        "rounds": int(content.get("rounds") or 3),
    }


async def _handle_conference(args: dict) -> dict:
    """Run a brainstorm conference: several models argue the topic over rounds,
    each reading the running transcript, then a rapporteur maps the disagreement.

    ``models`` picks the bench (default: the available subset of
    fable/opus/deepseek/minimax/glm); ``rounds`` is 1–6 (default 3); ``synthesizer``
    writes the closing map (default 'fable'). Single-turn (no session resume);
    ``session`` only groups the turn in the cross-agent hub."""
    topic = str(args.get("question") or args.get("topic") or "").strip()
    if not topic:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": "a conference needs a `question` (the topic to argue)",
        }
    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    if ref_fail is not None:
        return ref_fail
    hub_session = str(args.get("session") or "").strip() or "ask_conference"
    allowed, reason = _guard_check(topic, context, trusted=_resolve_trusted(args))
    if not allowed:
        audit.record(
            decision="denied",
            stage="guard",
            reason=reason,
            question=topic,
            context=context,
            session="conference",
            model="conference",
        )
        return _guard_refusal(reason)

    requested = args.get("models") or [m for m in conference.CANDIDATES if oracles.available(m)]
    resolved, unknown = oracles.resolve(requested)
    # An explicit bench gets the availability check the default one has: a model with
    # no key/CLI (or one the operator disabled) would only fail every turn, so it is
    # flagged up front instead of silently taking a seat that never speaks.
    selected = [k for k in resolved if oracles.available(k)]
    # A seat the OPERATOR turned off is a deliberate choice, not a missing answer, so
    # it is reported separately and left out of the quorum denominator — exactly how
    # `_council` treats `disabled`. Everything else that cannot answer (no key, no
    # CLI) is a seat that was asked for and did not speak.
    conf_disabled = [k for k in resolved if k not in selected and oracles.is_disabled(k)]
    unavailable = [k for k in resolved if k not in selected and k not in conf_disabled]
    if len(selected) < 2:
        return {
            "status": "error",
            "kind": "no_models",
            "detail": (
                f"a conference needs at least two available models; {len(selected)} "
                "available — configure keys or pass `models`."
            ),
            "unknown": list(unknown),
            "unavailable": unavailable,
        }
    rounds = max(1, min(int(args.get("rounds") or 3), 10))
    synth_tok, synth_err = _resolve_synth_token(str(args.get("synthesizer") or ""))
    if synth_err is not None:
        return {"status": "error", "kind": "bad_args", "detail": synth_err}
    synthesizer = synth_tok or "fable"

    trace_runtime.set_orchestration(
        mode="conference",
        models=list(selected),
        unknown=list(unknown),
        synthesizer=synthesizer,
    )
    t0 = time.monotonic()
    outcome = await conference.run_conference(
        topic,
        list(selected),
        context=context,
        rounds=rounds,
        synthesizer=synthesizer,
        max_parallel=_max_parallel(),  # bounds the blind round's fan-out, as a council's
        # `is not False` honoured only a real JSON false: a client that stringifies
        # booleans ("false") or sends 0 paid for the two calls it asked to skip.
        attack_premise=str(args.get("attack_premise", True)).strip().lower()
        not in ("false", "0", "none", ""),
    )
    duration_ms = int((time.monotonic() - t0) * 1000)
    spoke = len(outcome.speakers)
    # L3: a seat nothing recognized, or one that was unreachable, was ASKED FOR and did
    # not speak — it belongs in the denominator, the way the council counts it. Leaving
    # it out reported `["fable","opus","gpt-5"]` as a full "2/2" while the council
    # called the same list "2/3, degraded".
    requested_seats = len(selected) + len(unknown) + len(unavailable)
    quorum = f"{spoke}/{requested_seats}"  # disabled seats deliberately excluded
    trace_runtime.set_orchestration(quorum=quorum, synth_fallback=outcome.map_error is not None)
    if not outcome.posts:
        audit.record(
            decision="error",
            stage=None,
            reason="no_answers",
            question=topic,
            context=context,
            session="conference",
            model="conference",
            duration_ms=duration_ms,
            outcome_detail="no participant produced a contribution",
        )
        return {
            "status": "error",
            "kind": "no_answers",
            "detail": "no participant produced a contribution",
            "models": outcome.models,
            "unavailable": unavailable,
            "errors": outcome.errors,
        }
    audit.record(
        decision="allowed",
        stage=None,
        reason="conference",
        question=topic,
        context=context,
        session="conference",
        model="conference",
        duration_ms=duration_ms,
        quorum=quorum,
        synth_fallback=outcome.map_error is not None,
    )
    # A conference is two or more voices: one that heard from fewer must not pass a
    # monologue off as a full discussion. Like a council short of quorum it is still
    # `status: "ok"` (clients and stats know no other success value), but it carries
    # `degraded` and a `detail` saying who was missing.
    monologue = spoke < 2
    # The map is the conference's conclusion; without one, the discussion stands in.
    answer = outcome.map_text or "\n\n".join(
        f"[{p['model']} · round {p['round']}] {p['text']}" for p in outcome.posts
    )
    answered_by = oracles.label(synthesizer) if outcome.map_text else "conference"
    saved = outputs.save(
        tool="ask_conference",
        model=answered_by,
        question=topic,
        answer=answer,
        context=context,
        session="conference",
        thinking=outcome.map_thinking,
        sources=[
            {
                "model": f"{p['model']} · round {p['round']}"
                + (f" · {p['role'].replace('_', ' ')}" if p.get("role") else ""),
                "status": "ok",
                "answer": p["text"],
            }
            for p in outcome.posts
        ]
        # The saved map names pseudonyms, so the legend has to travel with it or the
        # artifact describes participants the file cannot identify.
        + (
            [
                {
                    "model": "map legend",
                    "status": "ok",
                    "answer": "\n".join(
                        f"{pseudo} = {real}" for pseudo, real in sorted(outcome.map_legend.items())
                    ),
                }
            ]
            if outcome.map_legend
            else []
        )
        + outcome.errors
        + ([outcome.map_error] if outcome.map_error else []),
    )
    _hub_mirror(
        session_key=hub_session,
        question=topic,
        answer=answer,
        oracle=answered_by,
        status="ok",
        duration_ms=duration_ms,
    )
    payload = {
        "status": "ok",
        "topic": outcome.topic,
        "rounds": outcome.rounds,
        "models": outcome.models,
        "unknown": list(unknown),
        "unavailable": unavailable,
        **({"disabled": conf_disabled} if conf_disabled else {}),
        "transcript": outcome.transcript,
        "posts": outcome.posts,
        "map": outcome.map_text,
        # The map names participants by pseudonym (the rapporteur is normally also a
        # participant, so its copy of the transcript is anonymized) — this maps back.
        **({"map_legend": outcome.map_legend} if outcome.map_legend else {}),
        # The premise the openings shared and the seat that argued against it, so the
        # rapporteur's choice of target stays auditable.
        **({"premise": outcome.premise} if outcome.premise else {}),
        "errors": outcome.errors,
        # As in a council envelope: who actually spoke, and whether anyone fell silent.
        "quorum": quorum,
        "degraded": spoke < requested_seats,
        "saved": saved,
    }
    if monologue:
        # Same denominator as `quorum`, or one payload contradicts itself.
        payload["detail"] = f"only {spoke} of {requested_seats} participants spoke"
    if outcome.map_error:
        payload["map_error"] = outcome.map_error
    _add_refs(payload, ref_resolved, ref_missing)
    _add_thinking(payload, outcome.map_thinking)
    return payload


async def _handle_council(args: dict) -> dict:
    """Fan a question out to the selected models, then synthesize the answers.

    Single-turn (no session/resume). ``models`` (default ['fable','minimax'],
    plus 'deepseek' when its API key is configured — cheap-first) picks from
    ['fable','deepseek','minimax','glm','gemini','codex'] plus any 'ollama:<model>'
    token. ``synthesizer`` picks the model that reconciles the panel (default
    Fable; falls back to Fable when it is unavailable or fails). Returns the
    merged ``answer`` plus each oracle's raw answer under ``sources``; degrades to
    the lone answerer when only one responds, and refuses/errors only when none do.

    ``provider`` scopes the council to one gateway (``ollama``/``atlas``/
    ``openrouter``/``lmstudio``); when set, the provider's default panel, token
    prefix, adjudicator ladder and (for lmstudio) sequential serving apply, and
    ``tier`` is ignored. Omit it for a mixed council chosen by ``models``/``tier``.
    """
    provider = str(args.get("provider") or "").strip().lower()
    if provider:
        handler = _COUNCIL_PROVIDER_HANDLERS.get(provider)
        if handler is None:
            return {
                "status": "error",
                "kind": "bad_args",
                "detail": (
                    f"unknown council provider {provider!r}; choose one of "
                    f"{', '.join(_COUNCIL_PROVIDER_HANDLERS)}"
                ),
            }
        return await handler({**args, "provider": provider})
    question = str(args.get("question") or "")
    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    if ref_fail is not None:  # all refs missing + no other context — don't fan out to N models
        return ref_fail
    synth_tok, synth_err = _resolve_synth_token(str(args.get("synthesizer") or ""))
    if synth_err is not None:
        return {"status": "error", "kind": "bad_args", "detail": synth_err}
    models = args.get("models")
    if not models:  # no explicit list — expand the named tier preset
        models = oracles.tier_models(str(args.get("tier") or "default"))
    selected, unknown = oracles.resolve(models)
    if not selected:
        # An explicit list naming nothing we know is a caller error, not a request
        # for the default panel: answering with models nobody named would pass off
        # a full-quorum consensus on a council that was never asked for.
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": (
                f"no recognized model in `models` ({', '.join(unknown)}) — use one of "
                f"{', '.join(oracles.KNOWN)}, an alias, a group ('twin'), or a "
                "'<provider>:<model>' token (atlas/openrouter/ollama/lmstudio/ali); for "
                "bare 'vendor/model' ids pass `provider` ('atlas' or 'openrouter')"
            ),
            "unknown": list(unknown),
        }
    session = str(args.get("session") or "").strip() or None
    trace_runtime.set_orchestration(
        mode="council",
        models=list(selected),
        unknown=list(unknown),
        synthesizer=synth_tok or oracles.SYNTHESIZER,
    )
    return await _council(
        question,
        context,
        selected,
        unknown,
        "ask_council",
        ref_resolved,
        ref_missing,
        session=session,
        synthesizer=synth_tok,
        trusted=_resolve_trusted(args),
    )


async def _handle_ollama_council(args: dict) -> dict:
    """Fan a question out to several Ollama Cloud models; Fable (or an explicit
    ``synthesizer``) synthesizes them.

    ``models`` is a list of cloud model ids (an 'ollama:' prefix is optional).
    Same fan-out/synthesis contract as ``ask_council`` — Fable synthesizes even
    though it isn't a member of the council.
    """
    question = str(args.get("question") or "")
    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    if ref_fail is not None:
        return ref_fail
    raw = args.get("models") or ollama.council_models()  # default to the configured set
    selected: list[str] = []
    for m in raw:
        tok = str(m).strip().lower()
        if not tok:
            continue
        if not tok.startswith(oracles.OLLAMA_PREFIX):
            tok = oracles.OLLAMA_PREFIX + tok
        if oracles.ollama_model(tok) and tok not in selected:
            selected.append(tok)
    if not selected:
        return {"status": "error", "kind": "no_models", "detail": "no ollama models given"}
    synth_tok: str | None = None
    note = "default synthesizer"
    explicit = str(args.get("synthesizer") or "").strip()
    if explicit:
        synth_tok, synth_err = _resolve_synth_token(explicit)
        if synth_err is not None:
            return {"status": "error", "kind": "bad_args", "detail": synth_err}
        note = "explicit synthesizer"
    session = str(args.get("session") or "").strip() or None
    trace_runtime.set_orchestration(
        mode="council",
        models=list(selected),
        flavor="ollama",
        synthesizer=synth_tok or oracles.SYNTHESIZER,
    )
    return await _council(
        question,
        context,
        selected,
        [],
        "ask_ollama_council",
        ref_resolved,
        ref_missing,
        session=session,
        synthesizer=synth_tok,
        synth_note=note,
        trusted=_resolve_trusted(args),
    )


async def _handle_lms_council(args: dict) -> dict:
    """Ask several LM Studio models the SAME question, one at a time, then
    synthesize with Fable (or an explicit `synthesizer`).

    Sequential by design: one GPU serves one local model at a time, so the
    shared parallel fan-out would either need the swap machinery on every
    member or thrash. Each member we loaded is freed after its turn (members
    already resident before the run are left alone), so a panel larger than
    VRAM still completes and the box is left as found. Default members come
    from ``lmstudio.council_models()`` (config ``lmstudio_council`` /
    ``ASK_FABLE_LMSTUDIO_COUNCIL``); the default synthesizer is Fable.
    """
    question = str(args.get("question") or "")
    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    if ref_fail is not None:
        return ref_fail
    raw = args.get("models") or lmstudio.council_models()
    selected: list[str] = []
    for m in raw:
        tok = str(m).strip()
        if not tok:
            continue
        if not tok.lower().startswith(oracles.LMS_PREFIX):
            tok = oracles.LMS_PREFIX + tok
        if oracles.lmstudio_model(tok) and tok not in selected:
            selected.append(tok)
    if not selected:
        return {"status": "error", "kind": "no_models", "detail": "no LM Studio models given"}
    synth_tok: str | None = None
    note = "default synthesizer"
    explicit = str(args.get("synthesizer") or "").strip()
    if explicit:
        synth_tok, synth_err = _resolve_synth_token(explicit)
        if synth_err is not None:
            return {"status": "error", "kind": "bad_args", "detail": synth_err}
        note = "explicit synthesizer"
    session = str(args.get("session") or "").strip() or None
    trace_runtime.set_orchestration(
        mode="council",
        models=list(selected),
        flavor="lmstudio",
        synthesizer=synth_tok or oracles.SYNTHESIZER,
    )
    return await _council(
        question,
        context,
        selected,
        [],
        "ask_lms_council",
        ref_resolved,
        ref_missing,
        session=session,
        synthesizer=synth_tok,
        synth_note=note,
        sequential=True,
        trusted=_resolve_trusted(args),
    )


async def _handle_atlas_council(args: dict) -> dict:
    """Fan a question out to several Atlas Cloud models, then synthesize the answers.

    ``models`` is a list of Atlas model ids (an 'atlas:' prefix is optional);
    defaults to the configured set (``configure_atlas_council`` /
    ASK_FABLE_ATLAS_COUNCIL), else 3 featured catalog models, one per provider.
    Same fan-out/synthesis contract as ``ask_council``, but the adjudicator
    defaults GPT-first: the local `codex` CLI (GPT-5.6 Sol) when installed, else
    Atlas-hosted openai/gpt-5.6-sol, else Fable.
    """
    question = str(args.get("question") or "")
    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    if ref_fail is not None:
        return ref_fail
    raw = args.get("models") or atlas.council_models()  # explicit → configured set
    if not raw:  # nothing configured — derive a small panel from the live catalog
        cat = await asyncio.to_thread(atlas.catalog)
        if not cat.get("cloud_ok"):
            return {
                "status": "error",
                "kind": "no_models",
                "detail": "no atlas models given, none configured, and the Atlas catalog is "
                "unreachable — pass `models` or run configure_atlas_council",
            }
        raw = [it["model_id"] for it in cat.get("featured", [])[:3]]
    # Preserve id casing (Atlas ids are case-sensitive); prefix matching and the
    # de-dupe are case-insensitive, first-seen spelling wins.
    selected: list[str] = []
    seen: set[str] = set()
    for m in raw:
        tok = str(m).strip()
        if not tok:
            continue
        if tok.lower().startswith(oracles.ATLAS_PREFIX):
            tok = oracles.ATLAS_PREFIX + tok[len(oracles.ATLAS_PREFIX) :]
        else:
            tok = oracles.ATLAS_PREFIX + tok
        if oracles.atlas_model(tok) and tok.lower() not in seen:
            seen.add(tok.lower())
            selected.append(tok)
    if not selected:
        return {"status": "error", "kind": "no_models", "detail": "no atlas models given"}
    if not atlas.configured():
        # Grok-family tokens reroute to the operator's local `grok` CLI keylessly;
        # anything else needs the Atlas chat endpoint.
        reroutable = grok.available() and all(
            grok.looks_like_grok_model(oracles.atlas_model(tok) or "") for tok in selected
        )
        if not reroutable:
            return {
                "status": "error",
                "kind": "not_configured",
                "detail": "atlas not configured (set ASK_FABLE_ATLAS_API_KEY, or "
                "ATLASCLOUD_API_KEY which the Atlas Cloud MCP server already uses); only "
                "xai/grok-* members can run without it via the local grok CLI",
            }
    # Adjudicator ladder — GPT-5.6 Sol preferred, the local CLI over the Atlas-hosted model.
    explicit = str(args.get("synthesizer") or "").strip()
    synth_raw = explicit or atlas.synthesizer_token() or ""
    if synth_raw:
        synth_tok, synth_err = _resolve_synth_token(synth_raw)
        if synth_err is not None:
            return {"status": "error", "kind": "bad_args", "detail": synth_err}
        note = "explicit synthesizer" if explicit else "persisted atlas_synthesizer config"
    elif oracles.available("codex"):
        synth_tok, note = "codex", "local codex CLI (GPT-5.6 Sol) preferred over Atlas-hosted"
    elif atlas.configured():
        synth_tok = oracles.ATLAS_PREFIX + atlas.DEFAULT_ATLAS_SYNTH_MODEL
        note = "codex CLI not installed; using Atlas-hosted GPT-5.6 Sol"
    else:
        synth_tok, note = "fable", "codex CLI not installed and Atlas unconfigured"
    session = str(args.get("session") or "").strip() or None
    trace_runtime.set_orchestration(
        mode="council", models=list(selected), flavor="atlas", synthesizer=synth_tok
    )
    return await _council(
        question,
        context,
        selected,
        [],
        "ask_atlas_council",
        ref_resolved,
        ref_missing,
        session=session,
        synthesizer=synth_tok,
        synth_note=note,
        trusted=_resolve_trusted(args),
    )


# The provider-scoped council dispatch behind `ask_council(provider=…)` — each
# value is the existing dedicated handler, so the internal title (council cache
# key + hub-session default) and the sequential LM Studio machinery are preserved.
_COUNCIL_PROVIDER_HANDLERS = {
    "ollama": _handle_ollama_council,
    "atlas": _handle_atlas_council,
    "openrouter": _handle_openrouter_council,
    "lmstudio": _handle_lms_council,
}


async def _handle_chain(args: dict) -> dict:
    """Thread a question through an ORDERED pipeline of oracles, each stage refining
    the last — the relay counterpart to ``ask_council``. ``pipeline`` is a string like
    'm3 > glm > fable' (or an ordered ``models`` array); order matters and repeats are
    allowed. Defaults to 'minimax > fable' when none given."""
    question = str(args.get("question") or "")
    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    if ref_fail is not None:
        return ref_fail
    models = args.get("models")
    if not models:
        raw = str(args.get("pipeline") or "")
        models = (
            [t.strip() for t in raw.split(">") if t.strip()]
            if raw.strip()
            else ["minimax", "fable"]
        )
    selected, unknown = oracles.resolve_ordered(models)
    session = str(args.get("session") or "").strip() or None
    trace_runtime.set_orchestration(mode="chain", pipeline=list(selected), unknown=list(unknown))
    return await _chain(
        question,
        context,
        selected,
        unknown,
        "ask_chain",
        ref_resolved,
        ref_missing,
        session=session,
        trusted=_resolve_trusted(args),
    )


def _chain_drift(recs: list[str | None]) -> tuple[list[str], bool]:
    """Ordered recommendation trail across stages, and whether it materially flipped
    (some stage said 'apply' and another 'reject'). The chain analogue of the council's
    ``material_disagreement`` — but read ACROSS the sequence, so it exposes a stage that
    reversed a prior stage's call rather than rubber-stamping it."""
    trail = [r for r in recs if r]
    material = "apply" in set(trail) and "reject" in set(trail)
    return trail, material


async def _chain(
    question: str,
    context: str,
    selected: list[str],
    unknown: list[str],
    title: str,
    ref_resolved: list[str] | None = None,
    ref_missing: list[str] | None = None,
    *,
    session: str | None = None,
    trusted: bool = False,
) -> dict:
    ref_resolved, ref_missing = ref_resolved or [], ref_missing or []
    hub_session = (session or "").strip() or title
    labels = [oracles.label(k) for k in selected]
    rep = Reporter(title + " · " + " → ".join(labels))
    rep.info("question", _preview(question))
    if unknown:
        rep.warn(f"ignoring unknown models: {', '.join(unknown)}")
    if not selected:
        rep.fail("no recognized models in the pipeline")
        rep.footer()
        return _add_panel_gaps(
            {
                "status": "error",
                "kind": "no_models",
                "detail": "no recognized models in the pipeline",
            },
            unknown,
        )

    allowed, reason = _guard_check(question, context, trusted=trusted)
    if not allowed:
        rep.fail(f"guard denied: {reason}")
        rep.footer()
        audit.record(
            decision="denied",
            stage="guard",
            reason=reason,
            question=question,
            context=context,
            session="chain",
            model="chain",
        )
        return _guard_refusal(reason)
    rep.ok("guard passed")

    # Order-sensitive cache key: join the pipeline into ONE token so cache.key's internal
    # sort can't reorder it — 'm3 > glm > fable' must not collide with 'glm > m3 > fable'.
    # Dropped unknown stages join it too: the payload reports them, so a pipeline that
    # lost one must not share an entry with the one that never named it.
    ck, served = _cache_lookup(
        title,
        [" > ".join(selected), *(f"unknown:{u}" for u in unknown)],
        question,
        context,
        effort=oracles.panel_cache_effort(selected),
    )
    if served is not None:
        rep.ok(f"cache hit ({served['cache_age_s']}s old)")
        rep.footer()
        return served

    t0 = time.monotonic()
    n = len(selected)
    # An unknown stage was still ASKED for, so it counts toward `requested`: a
    # pipeline that silently lost one must not report every requested stage answered.
    requested = n + len(unknown)
    steps: list[dict] = []  # {idx, key, model, role, res, prose, sidecar}
    chain_timeout_s = _chain_timeout_s(n)
    # Hard cap on the WHOLE chain. The chain is sequential, so worst-case
    # wall time is N × per-stage timeout; this puts a single env-tunable bound
    # on it so a 10-stage pipeline can't pin a server for 20 minutes.
    try:
        async with asyncio.timeout(chain_timeout_s):
            for i, key in enumerate(selected):
                is_final = i == n - 1
                ok_prior = [s for s in steps if s["res"].status == "ok"]
                role = "drafter" if not ok_prior else "synthesize" if is_final else "critic"
                # Reconciled visibility: a middle (critic) stage sees only the immediately-
                # preceding draft; the final (synthesize) stage sees ALL prior stages as peers.
                if role == "synthesize":
                    prior = [
                        (f"Stage {j + 1}", s["prose"], s["res"].thinking)
                        for j, s in enumerate(ok_prior)
                    ]
                elif role == "critic":
                    last = ok_prior[-1]
                    prior = [("Prior stage", last["prose"], last["res"].thinking)]
                else:
                    prior = []

                rep.start(f"stage {i + 1}/{n}: {oracles.label(key)} ({role})")
                st0 = time.monotonic()
                res = await oracles.run(
                    key, compose_chain_step(question, prior, (i + 1, n), role), context
                )
                trace_runtime.record_stage(
                    "orchestration.provider",
                    res.status,
                    kind=trace_runtime.EventKind.ORCHESTRATION,
                    orchestration={
                        "mode": "chain",
                        "role": role,
                        "provider": res.model,
                        "stage": i + 1,
                    },
                )
                secs = time.monotonic() - st0
                prose, sc = sidecar.extract(res.text) if res.status == "ok" else (res.text, None)
                rep.think(f"{res.model} (stage {i + 1})", res.thinking)
                if res.status == "ok":
                    rep.ok(f"stage {i + 1} answered", secs)
                else:
                    rep.warn(f"stage {i + 1} {res.status} — skipped: {res.text}", secs)
                steps.append(
                    {
                        "idx": i,
                        "key": key,
                        "model": res.model,
                        "role": role,
                        "res": res,
                        "prose": prose,
                        "sidecar": sc,
                    }
                )
    except TimeoutError:
        # Whole-chain timeout. Surface a structured error with whatever stages
        # already completed (so a caller can see how far the pipeline got).
        completed = len(steps)
        rep.fail(
            f"chain exceeded ASK_FABLE_CHAIN_TIMEOUT={chain_timeout_s:.0f}s at stage {completed + 1}/{n}"
        )
        rep.footer()
        audit.record(
            decision="error",
            stage=None,
            reason="timeout",
            question=question,
            context=context,
            session="chain",
            model="chain",
            outcome_detail=f"chain timed out after {completed}/{n} stages "
            f"(ASK_FABLE_CHAIN_TIMEOUT={chain_timeout_s:.0f}s)",
        )
        return _add_panel_gaps(
            {
                "status": "error",
                "kind": "timeout",
                "detail": (
                    f"ask_chain exceeded ASK_FABLE_CHAIN_TIMEOUT={chain_timeout_s:.0f}s "
                    f"at stage {completed + 1}/{n}"
                ),
                "pipeline": labels,
                "stages": [
                    {
                        "stage": s["idx"] + 1,
                        "model": s["model"],
                        "role": s["role"],
                        "status": s["res"].status,
                        "recommendation": (s["sidecar"] or {}).get("recommendation"),
                        "confidence": (s["sidecar"] or {}).get("confidence"),
                    }
                    for s in steps
                ],
                "answered": sum(1 for s in steps if s["res"].status == "ok"),
                "requested": requested,
            },
            unknown,
        )

    duration_ms = int((time.monotonic() - t0) * 1000)
    ok_steps = [s for s in steps if s["res"].status == "ok"]

    # No stage answered — refuse if any refused (scope), else surface an error.
    if not ok_steps:
        rep.footer("no stage produced an answer")
        refused = next((s for s in steps if s["res"].status == "refused"), None)
        if refused is not None:
            audit.record(
                decision="refused",
                stage="model",
                reason=refused["res"].text,
                question=question,
                context=context,
                session="chain",
                model="chain",
                duration_ms=duration_ms,
            )
            return _model_refused_payload(refused["res"])
        err = next((s for s in steps if s["res"].status == "error"), steps[-1])
        audit.record(
            decision="error",
            stage=None,
            reason=err["res"].kind,
            question=question,
            context=context,
            session="chain",
            model="chain",
            duration_ms=duration_ms,
            outcome_detail=err["res"].text,
        )
        return _add_panel_gaps(
            {"status": "error", "kind": err["res"].kind, "detail": err["res"].text}, unknown
        )

    # Final answer = the last stage if it answered; else Fable reconciles the survivors
    # (mirrors the council's fable-fallback so a failed terminus still yields an answer).
    final_step = steps[-1]
    fallback = None
    if final_step["res"].status == "ok":
        answer, answered_by, head_thinking = (
            final_step["prose"],
            final_step["model"],
            final_step["res"].thinking,
        )
    elif oracles.is_disabled("fable"):
        # The rescue calls fable.run directly, beneath the denylist gate in
        # oracles.run — so a Fable the operator disabled is skipped here, and the
        # last surviving stage stands unsynthesized.
        rep.warn("final stage failed and Fable is disabled — using last survivor")
        last = ok_steps[-1]
        answer, answered_by, head_thinking = last["prose"], last["model"], last["res"].thinking
        fallback = "used last surviving stage (final stage failed; Fable is disabled)"
    else:
        rep.start(f"final stage failed — Fable reconciles {len(ok_steps)} survivor(s)")
        labelled = [
            (f"Stage {j + 1}", s["prose"], s["res"].thinking) for j, s in enumerate(ok_steps)
        ]
        ssink = (
            rep.stream_think(f"{fable.fable_model()} (synthesis)")
            if _flag("ASK_FABLE_STREAM_REASONING")
            else None
        )
        synth, ssec = await _timed(
            fable.run(
                # The rescue reconciles stage answers about the SAME code the stages
                # saw; without it the reconciler adjudicates on rhetoric alone.
                compose_synth(question, labelled, context=context),
                system_prompt=SYNTH_SYSTEM_PROMPT,
                **({"on_think": ssink} if ssink else {}),
            )
        )
        if oracles.placeholder_telemetry(synth):
            synth.telemetry = oracles.fallback_telemetry(
                key="fable",
                requested_model=fable.fable_model(),
                transport="sdk",
                wall_duration_ms=ssec * 1000,
                actual_model=synth.model,
                returncode=synth.returncode,
                thinking=synth.thinking,
            )
        trace_runtime.record_provider(
            synth.telemetry,
            synth.status,
            synth.thinking,
            kind=synth.kind,
            answer=synth.text if synth.status == "ok" else None,
        )
        trace_runtime.record_stage(
            "orchestration.synthesis",
            synth.status,
            kind=trace_runtime.EventKind.ORCHESTRATION,
            orchestration={
                "mode": "chain",
                "role": "fallback_synthesizer",
                "provider": fable.fable_model(),
            },
        )
        if synth.status == "ok":
            rep.ok("fallback synthesis complete", ssec)
            answer, answered_by, head_thinking = (
                sidecar.extract(synth.text)[0],
                fable.fable_model(),
                synth.thinking,
            )
            fallback = "synthesized survivors (final stage failed)"
        else:
            trace_runtime.record_stage(
                "orchestration.fallback",
                "degraded",
                kind=trace_runtime.EventKind.ORCHESTRATION,
                orchestration={"mode": "chain", "reason": "fallback_synthesis_unavailable"},
            )
            rep.warn(f"fallback synthesis unavailable ({synth.status}); using last survivor", ssec)
            last = ok_steps[-1]
            answer, answered_by, head_thinking = last["prose"], last["model"], last["res"].thinking
            fallback = "used last surviving stage (final stage + fallback synthesis failed)"

    trail, material = _chain_drift([(s["sidecar"] or {}).get("recommendation") for s in ok_steps])
    rep.footer(f"done — {len(ok_steps)}/{requested} stages answered")
    trace_runtime.set_orchestration(
        quorum=f"{len(ok_steps)}/{requested}",
        synth_fallback=fallback is not None,
    )
    audit.record(
        decision="allowed",
        stage=None,
        reason="chain",
        question=question,
        context=context,
        session="chain",
        model="chain",
        duration_ms=duration_ms,
        quorum=f"{len(ok_steps)}/{requested}",
        synth_fallback=fallback is not None,
    )
    saved = outputs.save(
        tool=title,
        model=answered_by,
        question=question,
        answer=answer,
        context=context,
        session="chain",
        thinking=head_thinking,
        sources=_dump_sources([s["res"] for s in steps]),
    )
    _hub_mirror(
        session_key=hub_session,
        question=question,
        answer=answer,
        oracle=answered_by,
        status="ok",
        duration_ms=duration_ms,
    )
    payload = {
        "status": "ok",
        "mode": "chain",
        "pipeline": labels,
        "answer": answer,
        "answered_by": answered_by,
        "stages": [
            {
                "stage": s["idx"] + 1,
                "model": s["model"],
                "role": s["role"],
                "status": s["res"].status,
                **({"partial": True} if is_partial(s["res"]) else {}),
                "recommendation": (s["sidecar"] or {}).get("recommendation"),
                "confidence": (s["sidecar"] or {}).get("confidence"),
            }
            for s in steps
        ],
        "recommendation_drift": trail,
        "material_drift": material,
        "answered": len(ok_steps),
        "requested": requested,
        "saved": saved,
    }
    if fallback:
        payload["fallback"] = fallback
    # H1: a stage the provider cut off is not a finished stage. Name them, and treat the
    # chain as degraded — the next stage reasoned from half its input.
    chain_partial = [s["model"] for s in steps if s["res"].status == "ok" and is_partial(s["res"])]
    if chain_partial:
        payload["partial"] = chain_partial
        payload["degraded"] = True
    _add_panel_gaps(payload, unknown)
    _add_refs(payload, ref_resolved, ref_missing)
    _add_thinking(payload, head_thinking)
    # F5/TR-3: a chain that fell back (a stage failed; the answer is from an earlier stage)
    # is degraded — don't freeze it for the TTL, or a re-ask serves the fallback for an hour.
    # H1: a cut-off stage is the same kind of degradation, so it is not pinned either.
    if not fallback and not chain_partial:
        cache.put(ck, trace_runtime.prepare_cache_store(payload))
    return payload


def _resolve_one(token: str, default: str) -> str | None:
    """Resolve a single proposer/opponent token to a canonical oracle key (applying
    the chain's aliases, e.g. 'm3'→minimax, 'gpt'→codex). Falls back to ``default``
    when empty; returns None if the token names no known oracle. Group tokens are
    rejected upstream by ``_group_slot_detail`` — reaching here with one would
    silently keep only its first member."""
    recognized, _ = oracles.resolve_ordered([token or default])
    return recognized[0] if recognized else None


async def _handle_debate(args: dict) -> dict:
    """Pit two models against each other over a structured claims ledger, then have a
    fresh third model adjudicate — the adversarial counterpart to
    ``ask_council``/``ask_chain``. ``proposer`` argues, ``opponent`` refutes, the
    proposer revises, and ``adjudicator`` (default Fable; any council token, e.g.
    'opus') rules on what's still contested."""
    question = str(args.get("question") or "")
    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    if ref_fail is not None:
        return ref_fail
    for role in ("proposer", "opponent", "adjudicator"):
        detail = _group_slot_detail(f"the {role}", str(args.get(role) or ""))
        if detail is not None:
            return {"status": "error", "kind": "bad_args", "detail": detail}
    proposer = _resolve_one(str(args.get("proposer") or ""), "fable")
    opponent = _resolve_one(str(args.get("opponent") or ""), "minimax")
    adjudicator = _resolve_one(str(args.get("adjudicator") or ""), oracles.SYNTHESIZER)
    if adjudicator is None:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": f"unknown adjudicator: {args.get('adjudicator')!r} — use one of "
            f"{', '.join(oracles.KNOWN)} or an alias ({', '.join(sorted(oracles.ALIASES))})",
        }
    try:
        rounds = int(args.get("rounds") or 1)
    except (TypeError, ValueError):
        rounds = 1
    rounds = max(1, min(2, rounds))
    session = str(args.get("session") or "").strip() or None
    trace_runtime.set_orchestration(
        mode="debate",
        proposer=proposer,
        opponent=opponent,
        rounds=rounds,
        adjudicator=adjudicator,
    )
    return await _debate(
        question,
        context,
        proposer,
        opponent,
        rounds,
        ref_resolved,
        ref_missing,
        session=session,
        adjudicator=adjudicator,
        trusted=_resolve_trusted(args),
    )


async def _debate_turn(
    key: str,
    role: str,
    question: str,
    context: str,
    rep,
    *,
    prior: str = "",
    ledger: str = "",
    round2: bool = False,
) -> dict:
    """Run one debate turn and split its text into clean prose + sidecar + ledger.
    Returns {key, model, role, res, prose, sc, ledger}."""
    rep.start(f"{role}: {oracles.label(key)}")
    t0 = time.monotonic()
    res = await oracles.run(
        key, compose_debate_step(question, role, prior=prior, ledger=ledger, round2=round2), context
    )
    trace_runtime.record_stage(
        "orchestration.provider",
        res.status,
        kind=trace_runtime.EventKind.ORCHESTRATION,
        orchestration={"mode": "debate", "role": role, "provider": res.model},
    )
    secs = time.monotonic() - t0
    if res.status == "ok":
        prose, sc = sidecar.extract(res.text)
        prose, led = debate_ledger.extract(prose)
        rep.ok(f"{role} answered", secs)
    else:
        prose, sc, led = res.text, None, None
        rep.warn(f"{role} {res.status}: {res.text}", secs)
    rep.think(f"{res.model} ({role})", res.thinking)
    return {
        "key": key,
        "model": res.model,
        "role": role,
        "res": res,
        "prose": prose,
        "sc": sc,
        "ledger": led,
    }


async def _debate(
    question: str,
    context: str,
    proposer: str | None,
    opponent: str | None,
    rounds: int,
    ref_resolved: list[str] | None = None,
    ref_missing: list[str] | None = None,
    *,
    session: str | None = None,
    adjudicator: str = "fable",
    trusted: bool = False,
) -> dict:
    ref_resolved, ref_missing = ref_resolved or [], ref_missing or []
    hub_session = (session or "").strip() or "ask_debate"
    title = "ask_debate"
    if proposer is None or opponent is None:
        miss = "proposer" if proposer is None else "opponent"
        return {"status": "error", "kind": "no_models", "detail": f"unrecognized {miss} model"}

    rep = Reporter(f"{title} · {oracles.label(proposer)} vs {oracles.label(opponent)}")
    rep.info("question", _preview(question))

    allowed, reason = _guard_check(question, context, trusted=trusted)
    if not allowed:
        rep.fail(f"guard denied: {reason}")
        rep.footer()
        audit.record(
            decision="denied",
            stage="guard",
            reason=reason,
            question=question,
            context=context,
            session="debate",
            model="debate",
        )
        return _guard_refusal(reason)
    rep.ok("guard passed")

    # The adjudicator is part of the identity of the debate — a Fable ruling and an
    # Opus ruling on the same pair are different answers, so it joins the cache key.
    # Left out when it's the default, so existing cached debates still hit.
    stamp = f"{proposer} vs {opponent} r{rounds}"
    if adjudicator != oracles.SYNTHESIZER:
        stamp += f" adj:{adjudicator}"
    ck, served = _cache_lookup(
        title,
        [stamp],
        question,
        context,
        effort=oracles.panel_cache_effort([proposer, opponent, adjudicator]),
    )
    if served is not None:
        rep.ok(f"cache hit ({served['cache_age_s']}s old)")
        rep.footer()
        return served

    t0 = time.monotonic()
    turns: list[dict] = []
    # Claim ids still contested right now. The round loop keeps this current so the
    # TIMEOUT path — which runs outside that loop — can report them (M8).
    still_open: list[str] = []

    def finish(
        answer: str,
        sc: dict | None,
        resolution: str,
        *,
        answered_by: str,
        head_thinking: str = "",
        open_ids: list[str] | None = None,
        decisive: str | None = None,
        low_effort: bool = False,
        flags: dict | None = None,
    ) -> dict:
        """Assemble the final debate payload — a DECISION plus a compact debate block;
        the full transcript goes to disk, not inline."""
        # Neither a stalemate, a failed adjudication nor a timed-out debate produced a
        # NEUTRAL verdict, so none may carry the proposer's own (often "high") confidence.
        if resolution in ("stalemate", "adjudicator_unavailable", "degraded_timeout") and sc:
            sc = {**sc, "confidence": debate_ledger.downgrade_confidence(sc.get("confidence"))}
        drift = [
            {
                "role": t["role"],
                "model": t["model"],
                "recommendation": (t["sc"] or {}).get("recommendation"),
            }
            for t in turns
            if t["res"].status == "ok"
        ]
        duration_ms = int((time.monotonic() - t0) * 1000)
        rep.footer(f"done — {resolution} ({len(turns)} turns)")
        trace_runtime.set_orchestration(
            quorum=resolution,
            synth_fallback=resolution.startswith("degraded")
            or resolution == "adjudicator_unavailable",
        )
        audit.record(
            decision="allowed",
            stage=None,
            reason="debate",
            question=question,
            context=context,
            session="debate",
            model="debate",
            duration_ms=duration_ms,
            quorum=resolution,
            synth_fallback=resolution.startswith("degraded")
            or resolution == "adjudicator_unavailable",
        )
        saved = outputs.save(
            tool=title,
            model=answered_by,
            question=question,
            answer=answer,
            context=context,
            session="debate",
            thinking=head_thinking,
            sources=_dump_sources([t["res"] for t in turns]),
        )
        debate_block = {
            "pairing": [oracles.label(proposer), oracles.label(opponent)],
            "rounds": rounds,
            "resolution": resolution,
            "contested_claims_remaining": len(open_ids or []),
            "recommendation_drift": drift,
            "low_effort_opposition": low_effort,
            # A debate cut short with claims still contested IS a material
            # disagreement — the agent-facing skills key on this field.
            "material_disagreement": resolution
            in ("stalemate", "adjudicated", "adjudicator_unavailable")
            or (resolution == "degraded_timeout" and bool(open_ids)),
        }
        if decisive:
            debate_block["decisive_argument"] = decisive
        if flags:
            debate_block.update(flags)
        _hub_mirror(
            session_key=hub_session,
            question=question,
            answer=answer,
            oracle=answered_by,
            status="ok",
            duration_ms=duration_ms,
        )
        payload = {
            "status": "ok",
            "mode": "debate",
            "answer": answer,
            "answered_by": answered_by,
            "sidecar": sc,
            "missing_sidecar": sc is None,
            "debate": debate_block,
            "turns": [
                {
                    "role": t["role"],
                    "model": t["model"],
                    "status": t["res"].status,
                    **({"partial": True} if is_partial(t["res"]) else {}),
                    "recommendation": (t["sc"] or {}).get("recommendation"),
                    "confidence": (t["sc"] or {}).get("confidence"),
                }
                for t in turns
            ],
            "saved": saved,
        }
        # H1: the verdict itself was cut off at the output cap — say so, and below,
        # don't pin it for the TTL.
        debate_partial = [
            t["model"] for t in turns if t["res"].status == "ok" and is_partial(t["res"])
        ]
        if debate_partial:
            payload["partial"] = debate_partial
        _add_refs(payload, ref_resolved, ref_missing)
        _add_thinking(payload, head_thinking)
        # F5/TR-3: a degraded debate — adjudicator unavailable or a fallback to an earlier
        # answer — must not be frozen for the full TTL as if a real ruling occurred; a re-ask
        # should re-run. (Same marker as the synth_fallback flag above; stalemate/adjudicated
        # are real verdicts and stay cacheable.)
        if not (
            resolution.startswith("degraded")
            or resolution == "adjudicator_unavailable"
            or debate_partial
        ):
            cache.put(ck, trace_runtime.prepare_cache_store(payload))
        return payload

    async def adjudicate(
        open_ids: list[str], resolution: str, low_effort: bool, flags: dict | None = None
    ) -> dict:
        """Fresh, anonymized ruling by the adjudicator over the still-contested claims.
        On failure, fall back to the best answer we already have so the call never
        fails outright."""
        p, r, v, rb = turns[0], (turns[1] if len(turns) > 1 else None), None, None
        for t in turns:
            if t["role"] == "revise" and t["res"].status == "ok":
                v = t
        for t in turns[2:]:  # a refute-role turn after the round-1 refute is the rebut
            if t["role"] == "refute" and t["res"].status == "ok":
                rb = t
        ledger_text = debate_ledger.render_for_adjudicator(
            p["ledger"],
            r["ledger"] if r else None,
            v["ledger"] if v else None,
            open_ids,
            rebut_l=rb["ledger"] if rb else None,
        )
        jt = await _debate_turn(
            adjudicator, "adjudicate", question, context, rep, ledger=ledger_text
        )
        turns.append(jt)
        if jt["res"].status == "ok":
            decisive = (jt["ledger"] or {}).get("decisive_argument")
            return finish(
                jt["prose"],
                jt["sc"],
                resolution,
                answered_by=jt["model"],
                head_thinking=jt["res"].thinking,
                open_ids=open_ids,
                decisive=decisive,
                low_effort=low_effort,
                flags=flags,
            )
        # Adjudicator unavailable — fall back to the best answer we already have so the
        # call never fails outright, but DO NOT label it "adjudicated": that answer is
        # the proposer's own (revised or original), so no neutral party decided. Report
        # resolution="adjudicator_unavailable" and downgrade its confidence (via finish)
        # so a caller reading the top-level verdict isn't told a self-judged answer was
        # adjudicated. The prior disclosure was only a nested flags.adjudication_failed.
        best = v or p
        f2 = {**(flags or {}), "adjudication_failed": jt["res"].kind or jt["res"].status}
        return finish(
            best["prose"],
            best["sc"],
            "adjudicator_unavailable",
            answered_by=best["model"],
            head_thinking=best["res"].thinking,
            open_ids=open_ids,
            low_effort=low_effort,
            flags=f2,
        )

    debate_timeout_s = _chain_timeout_s(2 + rounds * 2)  # propose+refute(+rebut)+revise+adjudicate
    try:
        async with asyncio.timeout(debate_timeout_s):
            # --- PROPOSE -----------------------------------------------------
            p = await _debate_turn(proposer, "propose", question, context, rep)
            turns.append(p)
            if p["res"].status != "ok":
                rep.footer("proposer produced no position")
                audit.record(
                    decision=p["res"].status,
                    stage="model",
                    reason=p["res"].text,
                    question=question,
                    context=context,
                    session="debate",
                    model="debate",
                )
                if p["res"].status == "refused":
                    return _model_refused_payload(p["res"])
                return {"status": "error", "kind": p["res"].kind, "detail": p["res"].text}

            # --- REFUTE ------------------------------------------------------
            claims_txt = f"{p['prose']}\n\nCLAIMS:\n{debate_ledger.render_claims(p['ledger'])}"
            r = await _debate_turn(opponent, "refute", question, context, rep, prior=claims_txt)
            turns.append(r)
            if r["res"].status != "ok":  # opponent unconfigured / errored / refused → single voice
                return finish(
                    p["prose"],
                    p["sc"],
                    "degraded_single_critic",
                    answered_by=p["model"],
                    head_thinking=p["res"].thinking,
                    flags={"degraded_reason": r["res"].kind or r["res"].status},
                )
            if r["ledger"] is None:  # can't read the ledger — let Fable decide on the prose
                return await adjudicate(
                    [], "adjudicated", False, flags={"ledger_unparseable": "refute"}
                )
            contested = debate_ledger.contested_after_refute(r["ledger"])
            still_open[:] = contested  # so a timeout below can report what was open
            low_effort = debate_ledger.low_effort_opposition(r["ledger"])
            if not contested:  # opponent conceded everything — the proposal stands
                return finish(
                    p["prose"],
                    p["sc"],
                    "conceded",
                    answered_by=p["model"],
                    head_thinking=p["res"].thinking,
                    low_effort=low_effort,
                )

            # --- REVISE ------------------------------------------------------
            own = debate_ledger.render_claims(p["ledger"])
            disp = debate_ledger.render_dispositions(r["ledger"])
            v = await _debate_turn(
                proposer, "revise", question, context, rep, prior=disp, ledger=own
            )
            turns.append(v)
            if v["res"].status != "ok" or v["ledger"] is None:
                return await adjudicate(
                    contested,
                    "adjudicated",
                    low_effort,
                    flags={"ledger_unparseable": "revise"} if v["ledger"] is None else None,
                )
            open_ids = debate_ledger.open_after_revise(contested, v["ledger"])
            still_open[:] = open_ids
            recs_match = (
                r["sc"]
                and v["sc"]
                and r["sc"].get("recommendation") == v["sc"].get("recommendation")
            )
            if not open_ids and recs_match:  # all contests resolved AND both sides now agree
                return finish(
                    v["prose"],
                    v["sc"],
                    "converged",
                    answered_by=v["model"],
                    head_thinking=v["res"].thinking,
                    low_effort=low_effort,
                )
            if not (open_ids and rounds >= 2):
                return await adjudicate(open_ids, "adjudicated", low_effort)

            # --- REBUT (round 2) --------------------------------------------
            rebut_prior = (
                f"{v['prose']}\n\nSTILL-OPEN CLAIMS:\n"
                f"{debate_ledger.render_open_claims(p['ledger'], open_ids, r['ledger'])}"
            )
            rb = await _debate_turn(
                opponent, "refute", question, context, rep, prior=rebut_prior, round2=True
            )
            turns.append(rb)
            if rb["res"].status != "ok" or rb["ledger"] is None:
                return await adjudicate(open_ids, "adjudicated", low_effort)
            if not debate_ledger.still_contested_after_rebut(rb["ledger"]):
                return finish(
                    v["prose"],
                    v["sc"],
                    "converged",
                    answered_by=v["model"],
                    head_thinking=v["res"].thinking,
                    low_effort=low_effort,
                )
            if debate_ledger.is_stalemate(rb["ledger"], open_ids):
                return await adjudicate(open_ids, "stalemate", low_effort)
            return await adjudicate(open_ids, "adjudicated", low_effort)
    except TimeoutError:
        completed = len(turns)
        rep.fail(f"debate exceeded {debate_timeout_s:.0f}s after {completed} turn(s)")
        rep.footer()
        audit.record(
            decision="error",
            stage=None,
            reason="timeout",
            question=question,
            context=context,
            session="debate",
            model="debate",
            outcome_detail=f"debate timed out after {completed} turns",
        )
        # Degrade to the best answer produced so far rather than failing outright: the
        # proposer's latest position (revised, else original) — the same `v or p` the
        # adjudicator-failure path uses. Never the last ok turn as such: that can be the
        # opponent's refutation or rebuttal, an attack on the answer rather than one.
        own = [
            t for t in turns if t["role"] in ("propose", "revise") and t["res"].status == "ok"
        ]
        if own:
            best = own[-1]
            # M8: the clock ran out, it did not settle the argument. Carrying the open
            # claims out means the verdict reports `contested_claims_remaining` and
            # `material_disagreement` honestly instead of reading like a clean win.
            return finish(
                best["prose"],
                best["sc"],
                "degraded_timeout",
                answered_by=best["model"],
                head_thinking=best["res"].thinking,
                open_ids=list(still_open),
                flags={"timed_out_after_turns": completed},
            )
        return {
            "status": "error",
            "kind": "timeout",
            "detail": f"ask_debate exceeded {debate_timeout_s:.0f}s before any turn answered",
        }


async def _council(
    question: str,
    context: str,
    selected: list[str],
    unknown: list[str],
    title: str,
    ref_resolved: list[str] | None = None,
    ref_missing: list[str] | None = None,
    *,
    session: str | None = None,
    synthesizer: str | None = None,
    synth_note: str | None = None,
    sequential: bool = False,
    trusted: bool = False,
) -> dict:
    ref_resolved, ref_missing = ref_resolved or [], ref_missing or []
    synth_key = synthesizer or oracles.SYNTHESIZER
    hub_session = (session or "").strip() or title
    # The operator's denylist drops a member before anything counts it: a disabled
    # seat is a deliberate choice, not a missing answer, so it neither degrades the
    # quorum nor rules out a 'strong' consensus. An UNKNOWN token is the opposite —
    # a seat the caller asked for that never ran — so it counts as requested, and a
    # panel that lost one reads as degraded instead of full quorum.
    disabled = [k for k in selected if oracles.is_disabled(k)]
    selected = [k for k in selected if k not in disabled]
    requested = len(selected) + len(unknown)
    rep = Reporter(title + " · " + " + ".join(oracles.label(k) for k in selected))
    rep.info("question", _preview(question))
    if unknown:
        rep.warn(f"ignoring unknown models: {', '.join(unknown)}")
    if disabled:
        rep.warn(f"skipping models disabled by the operator: {', '.join(disabled)}")
        trace_runtime.set_orchestration(models=list(selected), disabled=list(disabled))
    if not selected:
        rep.fail("every model on the panel is disabled")
        rep.footer()
        return _add_panel_gaps(
            {
                "status": "error",
                "kind": "disabled",
                "detail": (
                    f"every model on this council is disabled by the operator "
                    f"({oracles.DISABLED_KEY}) — name others in `models`, or re-enable "
                    "one with the configure_disabled tool"
                ),
            },
            unknown,
            disabled,
        )

    allowed, reason = _guard_check(question, context, trusted=trusted)
    if not allowed:
        rep.fail(f"guard denied: {reason}")
        rep.footer()
        audit.record(
            decision="denied",
            stage="guard",
            reason=reason,
            question=question,
            context=context,
            session="council",
            model="council",
        )
        return _guard_refusal(reason)
    rep.ok("guard passed")

    # A non-default synthesizer produces a different merged answer, so it joins
    # the cache key; the default key stays byte-identical for cache continuity.
    # So do the seats that did not run, which the payload reports: a panel that
    # lost one must not share an entry with the panel that never named it.
    cache_models = [
        *selected,
        *([] if synth_key == oracles.SYNTHESIZER else [f"synth:{synth_key}"]),
        *(f"unknown:{u}" for u in unknown),
        *(f"disabled:{d}" for d in disabled),
    ]
    ck, served = _cache_lookup(
        title,
        cache_models,
        question,
        context,
        # The synthesizer runs at its own default too, so it belongs in the key —
        # without it, flipping ASK_FABLE_CODEX_REASONING still served the stale
        # merged verdict from a codex-synthesized council for the TTL.
        effort=oracles.panel_cache_effort([*selected, synth_key]),
    )
    if served is not None:
        rep.ok(f"cache hit ({served['cache_age_s']}s old)")
        rep.footer()
        return served

    rep.start(
        "asking "
        + ", ".join(oracles.label(k) for k in selected)
        + (" one at a time" if sequential else " in parallel")
    )
    t0 = time.monotonic()
    council_timeout = _council_timeout_s()
    timed: list[tuple] = []
    initial: set[str] = set()
    # LM Studio models we loaded and could NOT free again (L9). Reported on the
    # payload rather than swallowed, because the docstring promises the box is left
    # as it was found.
    cleanup_failed: list[dict] = []
    if sequential:
        # Local GPU: serve one member at a time, and free each one WE loaded
        # before moving to the next, so a panel larger than VRAM needs none of
        # the swap machinery and the box is left as it was found. Members that
        # were already resident before the run are left alone. A member that
        # cannot fit (e.g. a pre-existing model blocks it) fails as its own
        # source and synthesis proceeds on the rest.
        initial = set(await asyncio.to_thread(lmstudio.resident_models))
        for k in selected:
            try:
                res, secs = await asyncio.wait_for(
                    _timed(oracles.run(k, question, context)), council_timeout
                )
            except TimeoutError:
                rep.warn(
                    f"{oracles.label(k)} exceeded ASK_FABLE_COUNCIL_TIMEOUT={council_timeout:.0f}s"
                )
                res, secs = (
                    oracles.OracleResult(
                        key=k,
                        status="error",
                        kind="timeout",
                        model=oracles.label(k),
                        text=f"exceeded ASK_FABLE_COUNCIL_TIMEOUT={council_timeout:.0f}s",
                    ),
                    council_timeout,
                )
            # Match the model the token RESOLVES to, not its spelling: run/unload
            # match fuzzily (case, @variant), so 'qwen/x' names a resident 'qwen/x@q4'.
            lmodel = oracles.lmstudio_model(k)
            if lmodel and lmstudio.resident_key(lmodel, initial) is None:
                try:
                    freed = await asyncio.to_thread(lmstudio.unload, lmodel)
                    # `unload` REPORTS a refusal (a chat is in flight) or a failure in
                    # its return value rather than raising; discarding it left the box
                    # not as we found it while the docstring promised otherwise.
                    if freed.get("status") != "ok":
                        cleanup_failed.append(
                            {"model": lmodel, "kind": freed.get("kind"), "detail": freed.get("detail")}
                        )
                        rep.warn(f"could not free {lmodel} after its turn: {freed.get('kind')}")
                except Exception as exc:  # noqa: BLE001 — cleanup must not fail the panel
                    cleanup_failed.append({"model": lmodel, "kind": "exception", "detail": str(exc)})
                    rep.warn(f"could not free {lmodel} after its turn: {exc}")
            timed.append((res, secs))
    else:
        # Bounded, per-oracle-attributed fan-out with a hard wall-clock cap.
        #
        # Concurrency: a Semaphore caps simultaneous bridges on the ``full`` tier so a
        # 12-model fan-out can't exhaust ``ulimit -n`` or saturate provider rate limits.
        # This bound is deliberately PER-COUNCIL: on the single-agent stdio transport a
        # second concurrent council is rare, and a module-global asyncio primitive would
        # fight the per-request event loop. Tune with ASK_FABLE_MAX_PARALLEL.
        #
        # Timeout: we use asyncio.wait (not wait_for over gather) so a cap breach
        # PRESERVES the oracles that already answered instead of discarding the whole
        # batch — pending tasks are cancelled and reported as per-oracle timeouts, and
        # synthesis proceeds on whoever finished. NOTE on cancellation: cancelling a task
        # stops its asyncio coroutine, but blocking work already handed to a thread
        # (subprocess CLIs / HTTP via asyncio.to_thread) runs to completion — bounded by
        # each backend's own inner timeout, not by this cap.
        sem = asyncio.Semaphore(_max_parallel())

        async def _bounded(k: str):
            return await _timed(oracles.run_bounded(sem, k, question, context))

        tasks = [asyncio.create_task(_bounded(k), name=k) for k in selected]
        try:
            _, pending = await asyncio.wait(tasks, timeout=council_timeout)
        except BaseException:
            # asyncio.wait does NOT cancel its input tasks when IT is cancelled, so an outer
            # cancellation (MCP client disconnect) here would ORPHAN the model tasks — they'd
            # run on, burning quota and holding _max_parallel slots. Cancel them on the way
            # out, then re-raise the original.
            for t in tasks:
                t.cancel()
            raise
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)  # let cancellation settle

        # Harvest results in ``selected`` order, attributing every outcome to its real
        # oracle key (a timed-out or crashed bridge must still show up as ITS OWN source,
        # not collapse into a shared placeholder).
        for k, t in zip(selected, tasks, strict=True):
            if t in pending:  # cancelled by the wall-clock cap
                rep.warn(
                    f"{oracles.label(k)} exceeded ASK_FABLE_COUNCIL_TIMEOUT={council_timeout:.0f}s"
                )
                timed.append(
                    (
                        oracles.OracleResult(
                            key=k,
                            status="error",
                            kind="timeout",
                            model=oracles.label(k),
                            text=f"exceeded ASK_FABLE_COUNCIL_TIMEOUT={council_timeout:.0f}s",
                        ),
                        council_timeout,
                    )
                )
                continue
            exc = t.exception()
            if exc is not None:
                # oracles.run is designed never to raise, so a leaked exception is a
                # contract violation. Convert Exceptions to a synthetic error source, but
                # NEVER swallow shutdown signals (CancelledError/SystemExit/KeyboardInterrupt
                # are BaseException-not-Exception).
                if not isinstance(exc, Exception):
                    raise exc
                rep.fail(f"{oracles.label(k)} bridge raised: {type(exc).__name__}: {exc}")
                timed.append(
                    (
                        oracles.OracleResult(
                            key=k,
                            status="error",
                            kind="sdk_error",
                            model=oracles.label(k),
                            text=f"{type(exc).__name__}: {exc}",
                        ),
                        0.0,
                    )
                )
                continue
            timed.append(t.result())

    for res, secs in timed:
        rep.think(res.model, res.thinking)
        if res.status == "ok":
            rep.ok(f"{res.model} answered", secs)
        elif res.status == "refused":
            rep.warn(f"{res.model} refused: {res.text}", secs)
        else:
            rep.fail(f"{res.model} error ({res.kind}): {res.text}", secs)

    results = [res for res, _ in timed]
    for result in results:
        trace_runtime.record_stage(
            "orchestration.provider",
            result.status,
            kind=trace_runtime.EventKind.ORCHESTRATION,
            orchestration={"mode": "council", "role": "panelist", "provider": result.model},
        )
    sources = {res.key: _source(res) for res in results}
    ok = [res for res in results if res.status == "ok"]

    def duration_ms() -> int:
        return int((time.monotonic() - t0) * 1000)

    # None answered — refuse if any refused (scope), else surface an error.
    if not ok:
        rep.footer("no answer from any oracle")
        refused = next((r for r in results if r.status == "refused"), None)
        if refused is not None:
            audit.record(
                decision="refused",
                stage="model",
                reason=refused.text,
                question=question,
                context=context,
                session="council",
                model="council",
                duration_ms=duration_ms(),
            )
            payload = _model_refused_payload(refused)
            payload["sources"] = sources
            return _add_panel_gaps(payload, unknown, disabled)
        err = next((r for r in results if r.status == "error"), results[0])
        audit.record(
            decision="error",
            stage=None,
            reason=err.kind,
            question=question,
            context=context,
            session="council",
            model="council",
            duration_ms=duration_ms(),
            outcome_detail=err.text,
        )
        return _add_panel_gaps(
            {"status": "error", "kind": err.kind, "detail": err.text, "sources": sources},
            unknown,
            disabled,
        )

    # Exactly one answered — return it directly, no synthesis needed.
    if len(ok) == 1:
        lone = ok[0]
        lone_answer = sidecar.extract(lone.text)[0]  # drop the panelist's sidecar block
        rep.footer(f"one oracle answered ({lone.model}); others unavailable")
        trace_runtime.set_orchestration(
            quorum=f"1/{requested}",
            consensus="unknown",
            synth_fallback=False,
        )
        audit.record(
            decision="allowed",
            stage=None,
            reason="single_oracle",
            question=question,
            context=context,
            session="council",
            model="council",
            duration_ms=duration_ms(),
            quorum=f"1/{requested}",
            consensus="unknown",
            synth_fallback=False,
        )
        saved = outputs.save(
            tool=title,
            model=lone.model,
            question=question,
            answer=lone_answer,
            context=context,
            session="council",
            thinking=lone.thinking,
            sources=_dump_sources(results),
        )
        _hub_mirror(
            session_key=hub_session,
            question=question,
            answer=lone_answer,
            oracle=lone.model,
            status="ok",
            duration_ms=duration_ms(),
        )
        payload = {
            "status": "ok",
            "mode": "council",
            "synthesizer": None,
            "saved": saved,
            "answer": lone_answer,
            "answered_by": lone.model,
            "sources": sources,
            "consensus": "unknown",
            "material_disagreement": False,
            **_council_envelope(ok, requested, None, consensus="unknown"),
        }
        _add_panel_gaps(payload, unknown, disabled)
        _add_refs(payload, ref_resolved, ref_missing)
        _add_thinking(payload, lone.thinking)
        # T3: a lone survivor is a degraded 1-of-N result — never cache it, or a transient
        # blip that felled the rest freezes the single-model answer for the whole TTL.
        return payload

    # Two or more answered — the synthesizer (Fable unless overridden) reconciles
    # them into one. Extract each panelist's (prose, sidecar) once; use
    # recommendations for a deterministic consensus signal and anonymize+reorder
    # for the synthesis.
    extracted = [(r, sidecar.extract(r.text)) for r in ok]
    recs = [sc["recommendation"] for _, (_, sc) in extracted if sc and sc.get("recommendation")]
    confs = [sc.get("confidence") for _, (_, sc) in extracted if sc and sc.get("recommendation")]
    # panel_size = how many ANSWERED (separates unknown/partial); requested = how many
    # were ASKED (gates 'strong'). A 2-of-5 unanimous survivors panel is 'partial', not
    # 'strong'. The vote vector shows the full-panel gap (non-answerers + dropped sidecars).
    consensus, material = _consensus(recs, len(ok), confs, requested=requested)
    # Independence gate: a 'strong' verdict is an independence claim, so a panel
    # that agreed but spans only one lab (shared training lineage → correlated
    # errors) is downgraded to 'partial'. The envelope carries `independent_labs`
    # and an explanatory next action; this keeps the top-level `consensus` field
    # itself honest for a caller that keys on it alone.
    if consensus == "strong" and oracles.distinct_labs([r.key for r in ok]) < 2:
        consensus = "partial"
    votes = _vote_vector(recs, requested)

    def synth_prompt_for(key: str) -> str:
        # The synthesizer may also be a panelist: present the panel anonymized and
        # with its own answer LAST — it can't favor its answer by name, and it
        # reads its own voice last, not first. Keyed per attempt so a Fable rescue
        # after a failed non-Fable synthesizer re-anonymizes correctly.
        ordered = [x for x in extracted if x[0].key != key] + [
            x for x in extracted if x[0].key == key
        ]
        labelled = [
            (f"Expert {chr(65 + i)}", prose, r.thinking)
            for i, (r, (prose, _)) in enumerate(ordered)
        ]
        return compose_synth(
            question, labelled, context=context, material_disagreement=material
        )

    synth_used: str | None = synth_key
    synth_fallback: str | None = None
    if synth_key != oracles.SYNTHESIZER and not oracles.available(synth_key):
        rep.warn(f"synthesizer {oracles.label(synth_key)} not available — falling back to Fable")
        synth_used, synth_fallback = oracles.SYNTHESIZER, "fable"
    rep.start(f"synthesizing with {oracles.label(synth_used)}")
    if material:
        rep.warn("panelists gave conflicting recommendations — forcing adjudication")

    async def _synthesize(key: str):
        if oracles.is_disabled(key):
            # The denylist outranks every rung of the ladder, Fable's included:
            # skip it before any call (so no provider event — nothing ran) and let
            # the ladder move on, or the council answer with its first panelist.
            rep.warn(f"synthesizer {oracles.label(key)} is disabled by the operator — skipped")
            return oracles.disabled_result(key), 0.0
        # Live reasoning comes from the Claude Agent SDK, so it works for ANY
        # Anthropic-backed synthesizer — Opus 5 rides the same SDK as Fable.
        # Testing `key == SYNTHESIZER` silently dropped the live trace whenever a
        # non-default synthesizer was chosen; other backends still print after.
        sink = (
            rep.stream_think(f"{oracles.label(key)} (synthesis)")
            if _flag("ASK_FABLE_STREAM_REASONING") and key in oracles._ANTHROPIC
            else None
        )
        res, secs = await _timed(oracles.run_synthesis(key, synth_prompt_for(key), on_think=sink))
        trace_runtime.record_provider(
            res.telemetry,
            res.status,
            res.thinking,
            kind=res.kind,
            answer=res.text if res.status == "ok" else None,
        )
        trace_runtime.record_stage(
            "orchestration.synthesis",
            res.status,
            kind=trace_runtime.EventKind.ORCHESTRATION,
            orchestration={
                "mode": "council",
                "role": "synthesizer",
                "provider": oracles.label(key),
            },
        )
        if sink is None:  # already streamed live otherwise
            rep.think(f"{oracles.label(key)} (synthesis)", res.thinking)
        return res, secs

    synth, ssec = await _synthesize(synth_used)
    if synth.status != "ok" and synth_used != oracles.SYNTHESIZER:
        trace_runtime.record_stage(
            "orchestration.fallback",
            "degraded",
            kind=trace_runtime.EventKind.ORCHESTRATION,
            orchestration={"mode": "council", "reason": "synthesizer_failed"},
        )
        rep.warn(
            f"{oracles.label(synth_used)} synthesis failed ({synth.status}) — retrying with Fable",
            ssec,
        )
        synth_used, synth_fallback = oracles.SYNTHESIZER, "fable"
        synth, ssec = await _synthesize(synth_used)

    if synth.status == "ok":
        rep.ok("synthesis complete", ssec)
        rep.footer(f"done — merged answer from {len(ok)} oracles")
        answer, synth_label = sidecar.extract(synth.text)[0], oracles.label(synth_used)
    else:
        trace_runtime.record_stage(
            "orchestration.fallback",
            "degraded",
            kind=trace_runtime.EventKind.ORCHESTRATION,
            orchestration={"mode": "council", "reason": "synthesis_unavailable"},
        )
        # Synthesis failed — fall back to the first oracle's answer, note it.
        rep.warn(f"synthesis unavailable ({synth.status}); returning {ok[0].model}'s answer", ssec)
        rep.footer("done — synthesis fell back to a single oracle")
        answer, synth_label = sidecar.extract(ok[0].text)[0], None
        synth_used, synth_fallback = None, "first_answer"

    if sequential:
        # A local synthesizer loaded for us is ours to free too (unless it was
        # already resident before the panel ran).
        lm_synth = oracles.lmstudio_model(synth_used or "")
        if lm_synth and lmstudio.resident_key(lm_synth, initial) is None:
            try:
                freed = await asyncio.to_thread(lmstudio.unload, lm_synth)
                if freed.get("status") != "ok":
                    cleanup_failed.append(
                        {"model": lm_synth, "kind": freed.get("kind"), "detail": freed.get("detail")}
                    )
            except Exception as exc:  # noqa: BLE001 — cleanup must not fail the council
                cleanup_failed.append({"model": lm_synth, "kind": "exception", "detail": str(exc)})

    audit.record(
        decision="allowed",
        stage=None,
        reason="council_synth" if synth_label else "council_synth_fallback",
        question=question,
        context=context,
        session="council",
        model="council",
        duration_ms=duration_ms(),
        quorum=f"{len(ok)}/{requested}",
        consensus=consensus,
        synth_fallback=synth_label is None,
    )
    trace_runtime.set_orchestration(
        quorum=f"{len(ok)}/{requested}",
        consensus=consensus,
        synthesizer=synth_key,
        synth_fallback=synth_label is None,
    )
    answered_by = synth_label or ok[0].model
    # Panelists (and the synthesis itself) whose answer the provider cut off.
    cut_off = _partial_models(ok) + ([synth.model] if is_partial(synth) else [])
    saved = outputs.save(
        tool=title,
        model=answered_by,
        question=question,
        answer=answer,
        context=context,
        session="council",
        thinking=synth.thinking,
        sources=_dump_sources(results),
    )
    _hub_mirror(
        session_key=hub_session,
        question=question,
        answer=answer,
        oracle=answered_by,
        status="ok",
        duration_ms=duration_ms(),
    )
    payload = {
        "status": "ok",
        "mode": "council",
        "synthesizer": synth_label,
        "synthesis": {
            "requested": synth_key,
            "used": synth_used,
            "fallback": synth_fallback,
            # T3: when synthesis failed entirely we return a raw panelist answer. Make
            # that a positive signal, not something a caller must infer from used=None.
            **({"answer_is_unsynthesized": True} if synth_label is None else {}),
            **({"note": synth_note} if synth_note else {}),
        },
        "saved": saved,
        "answer": answer,
        "sources": sources,
        "consensus": consensus,
        "material_disagreement": material,
        "consensus_votes": votes,
        **_council_envelope(
            ok,
            requested,
            synth_label,
            consensus=consensus,
            material_disagreement=material,
            partial=cut_off,
        ),
    }
    _add_panel_gaps(payload, unknown, disabled)
    _add_refs(payload, ref_resolved, ref_missing)
    _add_thinking(payload, synth.thinking if synth_label else ok[0].thinking)
    # T3: only cache a genuinely synthesized council answer. When synthesis failed entirely
    # (synth_label is None → a raw panelist fallback), don't freeze that degraded result.
    # H1: nor a PARTIAL one. A synthesis cut off at its output cap, or a merge built on a
    # panelist's cut-off answer, is served for the whole TTL as a complete verdict — the
    # same lie the per-oracle and single-model layers already refuse to pin.
    if synth_label is not None and not cut_off and not is_partial(synth):
        cache.put(ck, trace_runtime.prepare_cache_store(payload))
    # AFTER the cache write: whether we could free a model is host state at this
    # instant, not part of the answer. Stored, a later hit would replay "could not
    # free X" for the TTL on calls that touched no LM Studio model at all (L9).
    if cleanup_failed:
        payload["cleanup_failed"] = cleanup_failed
    return payload


async def _handle_list_ollama(args: dict) -> dict:
    """List the Ollama models available for the council, so the agent can offer a
    concrete choice. Best-effort discovery — a network failure still returns the
    configured council and any locally-pulled models, never an error."""
    refresh = bool(args.get("refresh", True))
    configured = ollama.council_models()
    rep = Reporter("list_ollama_models")
    if refresh:
        rep.start("discovering Ollama Cloud catalog")
        cat = await asyncio.to_thread(ollama.catalog)
        cloud, local, cloud_ok = cat["cloud"], cat["local"], cat["cloud_ok"]
        rep.ok(
            f"{len(cloud)} cloud · {len(local)} local pulled"
            if cloud_ok
            else f"cloud catalog unreachable · {len(local)} local pulled"
        )
    else:
        cloud, local, cloud_ok = [], [], False
    rep.info("configured council", ", ".join(configured) or "(none)")
    rep.footer()
    return {
        "status": "ok",
        "reachable": ollama.configured(),
        "endpoint": ollama.base_url(),
        "cloud_catalog_ok": cloud_ok,
        "available_cloud": cloud,
        "pulled_local": local,
        "configured_council": configured,
        "default_model": ollama.default_model(),
        "config_file": str(config.path()),
        "hint": "Ask the user which of `available_cloud` to include, then call "
        "configure_ollama_council with their picks.",
    }


async def _handle_list_lms(args: dict) -> dict:
    """List the models on the configured LM Studio server so the agent can offer
    a concrete choice. Best-effort: an unreachable server still returns the
    configured defaults, never an error."""
    refresh = bool(args.get("refresh", True))
    rep = Reporter("list_lms_models")
    cat = (
        await asyncio.to_thread(lmstudio.catalog)
        if refresh
        else {"ok": False, "endpoint": lmstudio.base_url(), "models": [], "error": ""}
    )
    models = cat.get("models") or []
    chat = [m for m in models if m.get("type") in ("llm", "vlm")]
    loaded = [
        {
            "model": m["key"],
            "context_length": m.get("loaded_context_length"),
            "max_context_length": m.get("max_context_length"),
            "size_bytes": m.get("size_bytes"),
            "instance_id": m.get("instance_id"),
        }
        for m in chat
        if m.get("loaded")
    ]
    if refresh:
        rep.start("discovering LM Studio models")
        rep.ok(
            f"{len(chat)} chat models · {len(loaded)} loaded"
            if cat.get("ok")
            else cat.get("error") or "unreachable"
        )
    rep.footer()
    # When the control page is reachable, classify the not-loaded models against
    # the live VRAM so an agent never offers one that cannot fit at all.
    room: dict = {}
    if refresh and cat.get("ok"):
        gpu_now = await asyncio.to_thread(controlpage.gpu)
        if gpu_now.get("available"):
            fits_now: list[str] = []
            needs_unload: list[str] = []
            too_large: list[str] = []
            unclassified: list[str] = []
            for m in chat:
                if m.get("loaded"):
                    continue
                buckets = {
                    "fits": fits_now,
                    "needs_room": needs_unload,
                    "too_large": too_large,
                }
                buckets.get(
                    lmstudio.room_verdict(m.get("size_bytes"), gpu_now), unclassified
                ).append(m["key"])
            room = {
                "gpu": gpu_now,
                "fits_now": fits_now,
                "needs_unload": needs_unload,
                "too_large": too_large,
            }
            if unclassified:
                room["unclassified"] = unclassified
    return {
        "status": "ok",
        "reachable": bool(cat.get("ok")),
        "endpoint": lmstudio.base_url(),
        "loaded": loaded,
        "loaded_bytes": sum(m.get("size_bytes") or 0 for m in chat if m.get("loaded")),
        "available": [m["key"] for m in chat if not m.get("loaded")],
        "default_model": lmstudio.default_model(),
        "context_default": lmstudio.context_default(),
        "context_ceiling": lmstudio.context_ceiling(),  # 0 = no per-host cap
        "output_cap": lmstudio.max_output_tokens(),
        "swap_policy": lmstudio.swap_policy(),
        "config_file": str(config.path()),
        "hint": "Pass one of these keys as `model` to ask_lms, or as an "
        "'lmstudio:<model>' token in ask_chain / ask_council. Do not offer models in "
        "`too_large` (they cannot fit the GPU at all); `needs_unload` ones need the "
        "operator's OK to free a resident first (see `unload_lms_model`). Under the "
        "default lmstudio_swap=never a load that does not fit returns an "
        "`unload_offer` naming what to free.",
        **room,
        **({"error": cat.get("error")} if cat.get("error") else {}),
    }


# The consolidated catalogue dispatch. `provider` selects which vendor listing to
# render; each branch is the existing per-provider lister.
_LIST_MODELS_PROVIDERS = {
    "ali": _handle_list_ali,
    "atlas": _handle_list_atlas,
    "openrouter": _handle_list_openrouter,
    "ollama": _handle_list_ollama,
    "lmstudio": _handle_list_lms,
}


async def _handle_list_models(args: dict) -> dict:
    """`list_models` — one catalogue, selected by `provider`."""
    raw = str(args.get("provider") or "").strip()
    handler = _LIST_MODELS_PROVIDERS.get(raw.lower())
    if handler is None:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": (
                f"unknown provider {raw!r}; choose one of "
                f"{', '.join(_LIST_MODELS_PROVIDERS)}"
            ),
        }
    return await handler({**args, "provider": raw.lower()})


async def _handle_unload_lms(args: dict) -> dict:
    """Operator-requested unload of one LM Studio model (frees memory).

    Never implicit: the caller is contractually responsible for having the
    operator's explicit consent (see the tool description). Refuses while the
    model is answering another ask_lms call, and waits for confirmation."""
    model = str(args.get("model") or "").strip()
    rep = Reporter(f"unload_lms_model · {model or '(none)'}")
    out = await asyncio.to_thread(lmstudio.unload, model)
    if out.get("status") == "ok":
        freed = out.get("freed_bytes")
        rep.ok(
            f"unloaded {out.get('model')} (freed {freed} bytes)"
            if freed
            else f"{out.get('model')}: nothing to unload"
        )
    else:
        rep.fail(f"unload failed ({out.get('kind')}): {out.get('detail')}")
    rep.footer()
    return out


async def _handle_host_status(args: dict) -> dict:
    """Read-only GPU/host snapshot from the operator's Control panel (lmstudio.example.com).

    Best-effort: an unreachable page is a clean error naming the URL and the
    override, never a crash. The GPU block feeds the ask_lms room check too."""
    rep = Reporter("host_status")
    snap = await asyncio.to_thread(controlpage.status)
    if snap is None:
        rep.fail(f"control page unreachable at {controlpage.base_url()}")
        rep.footer()
        return {
            "status": "error",
            "kind": "unreachable",
            "detail": f"control page unreachable at {controlpage.base_url()} "
            "(set ASK_FABLE_CONTROL_URL or config `control_page`)",
        }
    out = controlpage.summarize(snap)
    g = out["gpu"]
    rep.ok(
        f"GPU {g.get('util_pct')}% · VRAM {g.get('vram_used_mib')}/"
        f"{g.get('vram_total_mib')} MiB · {g.get('temp_c')}°C"
    )
    rep.footer()
    return {"status": "ok", "endpoint": controlpage.base_url(), **out}


async def _handle_diagnose(args: dict) -> dict:
    """Read-only health check of every backend — reachability, resolved model,
    breaker gate, and a fix line, rolled up. Makes no model call and never records
    a breaker outcome (see `diagnose.run`)."""
    rep = Reporter("diagnose")
    out = await diagnose.run()
    rep.ok(f"{out['rollup']} — {out['summary']}")
    rep.footer()
    return out


def _handle_configure_ollama(args: dict) -> dict:
    """Persist the user's Ollama council (and optional default model) to the config
    file so it survives across sessions and overrides the env defaults."""
    rep = Reporter("configure_ollama_council")
    patch: dict = {}

    if "models" in args and args.get("models") is not None:
        raw = args.get("models") or []
        if not isinstance(raw, list):
            return {"status": "error", "kind": "bad_args", "detail": "`models` must be a list"}
        # Strip any 'ollama:' prefix, normalize bare names to daemon-ready cloud ids
        # (minimax-m3 -> minimax-m3:cloud), then de-dupe order-preserving.
        stripped = ollama.dedupe_models([str(m) for m in raw if str(m).strip()])
        models = ollama.dedupe_models([ollama.cloud_id(m) for m in stripped])
        if not models:
            return {"status": "error", "kind": "bad_args", "detail": "`models` list is empty"}
        patch["ollama_council"] = models

    if args.get("default_model"):
        patch["ollama_model"] = ollama.cloud_id(str(args["default_model"]))

    if not patch:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": "nothing to configure — pass `models` and/or `default_model`",
        }

    saved = config.save(patch)
    if saved is None:
        rep.fail("config write failed")
        rep.footer()
        return {"status": "error", "kind": "write_failed", "detail": "could not write config file"}

    council = patch.get("ollama_council")
    rep.ok("saved council: " + ", ".join(council) if council else "config updated")
    rep.footer()
    return {
        "status": "ok",
        "saved_to": saved,
        "ollama_council": ollama.council_models(),
        "default_model": ollama.default_model(),
    }


def _handle_configure_atlas(args: dict) -> dict:
    """Persist the user's Atlas council (and optional synthesizer) to the config
    file so it survives across sessions and overrides the env defaults."""
    rep = Reporter("configure_atlas_council")
    patch: dict = {}

    if "models" in args and args.get("models") is not None:
        raw = args.get("models") or []
        if not isinstance(raw, list):
            return {"status": "error", "kind": "bad_args", "detail": "`models` must be a list"}
        models = atlas.dedupe_models([str(m) for m in raw if str(m).strip()])
        if not models:
            return {"status": "error", "kind": "bad_args", "detail": "`models` list is empty"}
        patch["atlas_council"] = models

    if args.get("synthesizer"):
        tok, err = _resolve_synth_token(str(args["synthesizer"]))
        if err is not None:
            return {"status": "error", "kind": "bad_args", "detail": err}
        patch["atlas_synthesizer"] = tok  # the resolved token ('gpt' persists as 'codex')

    if not patch:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": "nothing to configure — pass `models` and/or `synthesizer`",
        }

    saved = config.save(patch)
    if saved is None:
        rep.fail("config write failed")
        rep.footer()
        return {"status": "error", "kind": "write_failed", "detail": "could not write config file"}

    council = patch.get("atlas_council")
    rep.ok("saved council: " + ", ".join(council) if council else "config updated")
    rep.footer()
    return {
        "status": "ok",
        "saved_to": saved,
        "atlas_council": atlas.council_models(),
        "synthesizer": atlas.synthesizer_token(),
    }


# The provider-scoped council-config dispatch behind `configure_council(provider=…)`.
# Each value is the existing per-provider writer, so the config-file keys and the
# result payloads are unchanged.
_CONFIGURE_COUNCIL_HANDLERS = {
    "ollama": _handle_configure_ollama,
    "atlas": _handle_configure_atlas,
    "openrouter": _handle_configure_openrouter,
}


def _handle_configure_council(args: dict) -> dict:
    """`configure_council` — persist the default panel (and adjudicator / default
    model) for one provider's council, routing to the per-provider writer."""
    provider = str(args.get("provider") or "").strip().lower()
    handler = _CONFIGURE_COUNCIL_HANDLERS.get(provider)
    if handler is None:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": (
                f"unknown provider {provider!r}; choose one of "
                f"{', '.join(_CONFIGURE_COUNCIL_HANDLERS)}"
            ),
        }
    forwarded = {k: v for k, v in args.items() if k != "provider"}
    if provider == "ollama" and forwarded.get("synthesizer"):
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": (
                "`synthesizer` is only for provider='atlas'/'openrouter'; for ollama "
                "pass `default_model`"
            ),
        }
    if provider in ("atlas", "openrouter") and forwarded.get("default_model"):
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": "`default_model` is only for provider='ollama'",
        }
    return handler(forwarded)


def _handle_configure_tracing(args: dict) -> dict:
    """Persist reasoning-trace settings (trace mode + live streaming) to the config
    file so they can be toggled at runtime — no ~/.claude.json edit or restart. Config
    overrides the ASK_FABLE_TRACE_MODE / ASK_FABLE_STREAM_REASONING env defaults, and
    because both are read live per call the change takes effect on the next call."""
    rep = Reporter("configure_tracing")
    patch: dict = {}

    if args.get("trace_mode") is not None:
        mode = str(args["trace_mode"]).strip().lower()
        if mode not in ("safe", "full"):
            return {
                "status": "error",
                "kind": "bad_args",
                "detail": "`trace_mode` must be 'safe' or 'full'",
            }
        patch["ASK_FABLE_TRACE_MODE"] = mode

    if args.get("stream_reasoning") is not None:
        patch["ASK_FABLE_STREAM_REASONING"] = "1" if args["stream_reasoning"] else "0"

    if not patch:
        return {
            "status": "error",
            "kind": "bad_args",
            "detail": "nothing to configure — pass `trace_mode` and/or `stream_reasoning`",
        }

    saved = config.save(patch)
    if saved is None:
        rep.fail("config write failed")
        rep.footer()
        return {"status": "error", "kind": "write_failed", "detail": "could not write config file"}

    effective = {
        "trace_mode": (config.setting("ASK_FABLE_TRACE_MODE") or "safe").lower(),
        "stream_reasoning": _flag("ASK_FABLE_STREAM_REASONING"),
    }
    rep.ok(
        f"tracing: mode={effective['trace_mode']} stream_reasoning={effective['stream_reasoning']}"
    )
    rep.footer()
    return {"status": "ok", "saved_to": saved, **effective}


# Coarse provider names a denylist entry may name (beyond oracle keys/aliases), used
# only to flag a typo'd token in the tool result — the denylist stores whatever is
# given, so this never blocks an entry. There is deliberately no "anthropic": it maps
# to no provider (fable/opus are their own keys), so accepting it would suppress the
# typo warning while doing nothing. Disable the Anthropic models by name instead
# (`fable`, `opus`, `opus55`, …).
_DISABLE_PROVIDERS = ("atlas", "openrouter", "ollama", "lmstudio", "ali")


def _norm_disable(tok: object) -> str:
    """Lowercase a denylist token and fold aliases (m3->minimax), so the stored and
    checked forms match `oracles.disabled_tokens`. Provider names pass through."""
    t = str(tok).strip().lower()
    return oracles.ALIASES.get(t, t) if t else t


def _handle_configure_disabled(args: dict) -> dict:
    """Persist the operator's oracle/provider denylist to the config file so it can
    be toggled at runtime — no ~/.claude.json edit or restart. The config value
    overrides the ASK_FABLE_DISABLED env var, and because the denylist is read live
    per call the change takes effect on the next call.

    `set` replaces the whole list; otherwise `disable`/`enable` add/remove against
    the current effective denylist. With no mutating arg it just reports the list."""
    rep = Reporter("configure_disabled")
    current = set(oracles.disabled_tokens())
    mutating = any(args.get(k) is not None for k in ("set", "disable", "enable"))

    if not mutating:
        rep.ok(f"disabled: {sorted(current) or '(none)'}")
        rep.footer()
        return {"status": "ok", "disabled": sorted(current)}

    if args.get("set") is not None:
        new = {_norm_disable(t) for t in args["set"] if str(t).strip()}
    else:
        new = set(current)
        for t in args.get("disable") or []:
            if str(t).strip():
                new.add(_norm_disable(t))
        for t in args.get("enable") or []:
            new.discard(_norm_disable(t))

    recognized = set(oracles.KNOWN) | set(_DISABLE_PROVIDERS) | set(oracles.ALIASES)
    unknown = sorted(t for t in new if t not in recognized)

    # `set: []` stores None, removing the key so the env var (if any) becomes the
    # source again. An `enable` that empties the list stores [] — an explicit
    # "nothing disabled" — or the env var would re-disable what was just enabled.
    # Lists are stored sorted for a stable, diff-able config file.
    cleared = args.get("set") is not None and not new
    saved = config.save({oracles.DISABLED_KEY: None if cleared else sorted(new)})
    if saved is None:
        rep.fail("config write failed")
        rep.footer()
        return {"status": "error", "kind": "write_failed", "detail": "could not write config file"}

    effective = sorted(oracles.disabled_tokens())
    rep.ok(f"disabled: {effective or '(none)'}")
    rep.footer()
    out: dict = {"status": "ok", "saved_to": saved, "disabled": effective}
    if unknown:
        out["unknown_tokens"] = unknown
        out["note"] = (
            "these tokens match no known oracle key or provider name; stored anyway "
            "in case they're a gateway you use, but check for a typo"
        )
    return out


def _resolve_context(context: str, context_ref) -> tuple[str, list[str], list[str], list[str]]:
    """Merge stored blobs referenced by ``context_ref`` (a key or list of keys) with
    the inline ``context``. Returns (effective_context, resolved, missing, degraded).
    Stored blobs come first, each labelled, then the inline context.

    ``context_store.get`` returns None for BOTH "key absent" and "store unreachable/
    corrupt" (it swallows errors). Conflating them let a transient SQLite lock look
    like a typo'd key, so the call proceeded on whatever other context existed and the
    model answered blind. ``last_error()`` is set by the failing op and cleared at the
    start of every store operation, so read immediately after each get it tells the two
    apart per key:
    a degraded key goes in ``degraded`` (a hard retry), not ``missing`` (a fix-the-key)."""
    if isinstance(context_ref, str):
        keys = [context_ref.strip()] if context_ref.strip() else []
    elif isinstance(context_ref, list):
        keys = [str(k).strip() for k in context_ref if str(k).strip()]
    else:
        keys = []
    resolved: list[str] = []
    missing: list[str] = []
    degraded: list[str] = []
    parts: list[str] = []
    for k in keys:
        val = context_store.get(k)
        if val is None:
            if context_store.last_error() is not None:
                degraded.append(k)
            else:
                missing.append(k)
        else:
            resolved.append(k)
            parts.append(f"[context:{k}]\n{val}")
    if (context or "").strip():
        parts.append(context.strip())
    return "\n\n".join(parts), resolved, missing, degraded


def _missing_with_suggestions(missing: list[str]) -> list[dict]:
    """Enrich missing keys with nearest available keys — a missing ref is almost
    always a typo or stale name, so a did-you-mean turns a dead call into a one-shot fix."""
    avail = [e["key"] for e in context_store.entries()]
    out: list[dict] = []
    for k in missing:
        near = difflib.get_close_matches(k, avail, n=3)
        out.append({"key": k, "did_you_mean": near} if near else {"key": k})
    return out


def _prepare_context(
    args: dict, *, has_history: bool = False
) -> tuple[str, list[str], list[str], dict | None]:
    """Resolve ``context_ref`` and decide the sole hard-fail case.

    Returns (effective_context, resolved, missing, fail). ``fail`` is set in two cases:
    (1) ``store_degraded`` — a referenced blob could not be READ because the store
    errored (locked/corrupt); we must NOT proceed on other context, because the caller
    expects that blob and the model would answer blind against a transient failure that
    a retry fixes. (2) ``needs_context`` — refs were requested but every one was
    genuinely ABSENT AND no other context remains (no inline, no resolved refs, no
    session history). Every other combination proceeds (missing keys are reported, not
    fatal), identically for ask / single-model / councils."""
    eff, resolved, missing, degraded = _resolve_context(
        str(args.get("context") or ""), args.get("context_ref")
    )
    fail = None
    if degraded:
        fail = {
            "status": "store_degraded",
            "detail": "the context store could not be read (locked or corrupt); "
            "referenced context was not resolved — retry, do NOT treat these as "
            "missing keys or re-paste blindly",
            "degraded_keys": degraded,
            "store_error": context_store.last_error(),
            "store_path": context_store.db_path(),
        }
    elif missing and not (eff.strip() or has_history):
        fail = {
            "status": "needs_context",
            "detail": "every referenced context key was missing and no other context "
            "was provided — fix the key(s) and retry",
            "missing_keys": _missing_with_suggestions(missing),
        }
    return eff, resolved, missing, fail


def _report_refs(rep: Reporter, resolved: list[str], missing: list[str]) -> None:
    if resolved:
        rep.ok(f"pulled context: {', '.join(resolved)}")
    if missing:
        rep.warn(f"context_ref not found: {', '.join(missing)}")


def _likely_present(needs: list[str], context: str) -> list[str]:
    """Of the model's ``needs_context`` items, those a distinctive token of which
    already appears in the supplied context — i.e. the agent probably pasted it and
    should RE-READ rather than blindly re-paste (MiniMax's point: the paste itself is
    the other untrusted input)."""
    ctx = (context or "").lower()
    if not ctx:
        return []
    out: list[str] = []
    for item in needs:
        toks = [t.lower() for t in re.split(r"[^A-Za-z0-9_]+", str(item)) if len(t) >= 4]
        # Word-boundary match, NOT substring: "test" must not match "latest",
        # "auth" must not match "author" — a false "already pasted" would starve the loop.
        if any(re.search(rf"(?<![a-z0-9_]){re.escape(t)}(?![a-z0-9_])", ctx) for t in toks):
            out.append(item)
    return out


def _followup(sidecar: dict | None, context: str, session: str | None = None) -> dict | None:
    """A machine-readable next-step when the model wants more context: what to paste
    and where. Non-empty only when the sidecar lists ``needs_context``."""
    if not sidecar:
        return None
    needs = [str(x) for x in (sidecar.get("needs_context") or []) if str(x).strip()]
    if not needs:
        return None
    hint: dict = {"needs_context": needs}
    likely = _likely_present(needs, context)
    if likely:
        hint["likely_already_pasted"] = likely  # re-read your paste before re-asking
    where = f" and re-ask on session '{session}'" if session else " and re-ask"
    hint["how"] = (
        'paste these into `context` (or `context(op="write", …)` them and pass `context_ref`)'
        + where
        + (
            "; items under likely_already_pasted may already be in your paste — re-read first"
            if likely
            else ""
        )
    )
    return hint


def _add_refs(payload: dict, resolved: list[str], missing: list[str]) -> dict:
    """Attach ref-attribution to a result (also stored in the cache entry, so a
    later cache hit still tells the agent which blobs the answer was computed over)."""
    if resolved:
        payload["context_ref_resolved"] = resolved
    if missing:
        payload["context_ref_missing"] = missing
    return payload


def _handle_context_write(args: dict) -> dict:
    key = str(args.get("key") or "").strip()
    value = str(args.get("value") or "")
    description = str(args.get("description") or "").strip()
    rep = Reporter("context_write")
    if not key:
        return {"status": "error", "kind": "bad_args", "detail": "`key` is required"}
    if not value.strip():
        return {"status": "error", "kind": "bad_args", "detail": "`value` is empty"}
    ok = context_store.put(key, value, description)
    if not ok:
        rep.fail("store write failed")
        rep.footer()
        return {
            "status": "error",
            "kind": "write_failed",
            "detail": "could not write context store",
        }
    rep.ok(f"stored '{key}' ({len(value)} chars)")
    rep.footer()
    return {
        "status": "ok",
        "key": key,
        "bytes": len(value),
        "hint": f"reference it later with context_ref='{key}' on `ask`",
    }


def _handle_code_index(args: dict) -> dict:
    """Index the configured project root for `code_search` (incremental).

    Embeddings are opt-in and best-effort: with no embed host answering, the
    chunks are stored unembedded and search degrades to keyword ranking."""
    root = codeindex.project_root()
    if root is None:
        return {
            "version": 1,
            "status": "error",
            "kind": "not_configured",
            "detail": "no project_root configured (config `project_root` or "
            "ASK_FABLE_PROJECT_ROOT)",
        }
    return codeindex.index(root, force=bool(args.get("rebuild")))


def _handle_code_search(args: dict) -> dict:
    """Hybrid semantic+keyword search over the indexed project root."""
    query = str(args.get("query") or "").strip()
    if not query:
        return {
            "version": 1,
            "status": "error",
            "kind": "bad_args",
            "detail": "`query` is required",
        }
    root = codeindex.project_root()
    if root is None:
        return {
            "version": 1,
            "status": "error",
            "kind": "not_configured",
            "detail": "no project_root configured (config `project_root` or "
            "ASK_FABLE_PROJECT_ROOT)",
        }
    try:
        k = int(args.get("k") or 8)
    except (TypeError, ValueError):
        k = 8
    return codeindex.search(root, query, k=max(1, min(50, k)), rerank=bool(args.get("rerank")))


def _handle_context_pack(args: dict) -> dict:
    """Server-side context packing: read named repo files within the configured
    project root, budget them, and store the bundle on the context bus under `key`.
    Advisory of nothing — it either packs and stores, or reports why not. The result
    is versioned (public API) and always carries `complete`."""
    key = str(args.get("key") or "").strip()
    paths = args.get("paths")
    rep = Reporter("context_pack")
    if not key:
        return {"version": 1, "status": "error", "kind": "bad_args", "detail": "`key` is required"}
    specs = [str(p) for p in paths if str(p).strip()] if isinstance(paths, list) else []
    if not specs:
        return {
            "version": 1,
            "status": "error",
            "kind": "bad_args",
            "detail": "`paths` must be a non-empty list of path specs",
        }

    root = safe_fs.resolve_root()
    if root is None:
        rep.fail("no project_root configured")
        rep.footer()
        return {
            "version": 1,
            "status": "error",
            "kind": "not_configured",
            "hint": "set `project_root` in config or the ASK_FABLE_PROJECT_ROOT env var "
            "to enable context_pack",
        }

    max_chars = None
    raw_mc = args.get("max_chars")
    if raw_mc is not None:
        try:
            mc = int(raw_mc)
            max_chars = mc if mc > 0 else None
        except (TypeError, ValueError):
            max_chars = None

    extra = tuple(config.get_list("pack_blocklist") or ())
    res = resolver.pack(specs, root, max_chars=max_chars, extra_blocklist=extra)
    for s in res.skipped:
        rep.warn(f"skipped {s['spec']}: {s['reason']}")
    if not res.included:  # nothing admitted — leave the store untouched
        rep.fail("nothing packed (all specs rejected or over budget)")
        rep.footer()
        return {
            "version": 1,
            "status": "error",
            "kind": "empty_pack",
            "complete": False,
            "skipped": res.skipped,
            "detail": "no specs could be packed — nothing written to the store",
        }

    fingerprint = hashlib.sha256(json.dumps(specs, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    description = f"context_pack:{key}:{fingerprint}"
    # A one-line manifest at the head of the stored value tells any downstream reader
    # (and the oracle, via context_ref) whether the pack was complete.
    manifest = (
        f"[context_pack key='{key}' files={len(res.included)} "
        f"skipped={len(res.skipped)} complete={str(res.complete).lower()}]"
    )
    if not context_store.put(key, f"{manifest}\n\n{res.bundle}", description):
        rep.fail("store write failed")
        rep.footer()
        return {
            "version": 1,
            "status": "error",
            "kind": "store_failed",
            "detail": "could not write the packed context to the store",
        }

    rep.ok(
        f"packed {len(res.included)} file(s), {len(res.skipped)} skipped, "
        f"{res.chars} chars -> '{key}'"
    )
    rep.footer()
    return {
        "version": 1,
        "status": "ok",
        "key": key,
        "complete": res.complete,
        "files": res.included,
        "skipped": res.skipped,
        "chars": res.chars,
        "hint": f"reference it with context_ref='{key}' on `ask`",
    }


def _handle_context_read(args: dict) -> dict:
    """Read one blob by key, or LIST the bus when no key is given (read-only)."""
    key = str(args.get("key") or "").strip()
    if not key:
        return _handle_context_list(args)
    meta = context_store.get_meta(key)
    if meta is None:
        err = context_store.last_error()
        if err is not None:  # None conflates "key absent" with "store degraded" — disambiguate
            return {
                "status": "error",
                "kind": "store_unavailable",
                "detail": f"context store degraded; cannot confirm '{key}' exists (do not re-paste — fix the store)",
                "store_error": err,
                "db_path": context_store.db_path(),
            }
        return {
            "status": "error",
            "kind": "not_found",
            "detail": f"no context stored under '{key}'",
        }
    value, ts, description = meta
    return {
        "status": "ok",
        "key": key,
        "value": value,
        "bytes": len(value),
        "age_s": max(0, int(time.time() - ts)),
        "description": description,
    }


def _handle_help(args: dict) -> dict:
    """Serve the manual that does not fit in the truncated standing instructions.

    Free and local: no model call, no network, no audit spend. Cheap enough that
    an agent can call it speculatively rather than guess at an argument.
    """
    topic = str(args.get("topic") or "all").strip().lower() or "all"
    return {
        "status": "ok",
        "topic": topic,
        "help": help_text(topic),
        "topics": ["refused", "context", "setup", "tools", "all"],
    }


def _handle_context_list(_args: dict) -> dict:
    ents = context_store.entries()
    out = {"status": "ok", "count": len(ents), "entries": ents}
    err = context_store.last_error()
    if err is not None:  # an empty list may be a degraded store, not an empty bus — say which
        out["store_error"] = err
        out["db_path"] = context_store.db_path()
    return out


def _handle_context_delete(args: dict) -> dict:
    key = str(args.get("key") or "").strip()
    if not key:
        return {"status": "error", "kind": "bad_args", "detail": "`key` is required"}
    deleted = context_store.delete(key)
    return {"status": "ok", "key": key, "deleted": deleted}


def _handle_context(args: dict) -> dict:
    """`context` — the mutating half of the bus, dispatched by `op`
    (write/pack/delete). Kept apart from `context_read` so the read tool stays
    read-only while these carry an honest `destructiveHint`."""
    op = str(args.get("op") or "").strip().lower()
    if op == "write":
        return _handle_context_write(args)
    if op == "pack":
        return _handle_context_pack(args)
    if op == "delete":
        return _handle_context_delete(args)
    return {
        "status": "error",
        "kind": "bad_args",
        "detail": f"unknown context op {op!r}; choose one of write, pack, delete",
    }


async def _handle_reset(store: SessionStore, args: dict) -> dict:
    session = str(args.get("session") or "default")
    # `ask_opus5` namespaces its sessions, so clearing one needs the same prefix.
    # Resolve through ALIASES: an agent that learned 'opus' from the council docs
    # would otherwise silently clear the FABLE session instead of the Opus one.
    requested = str(args.get("model") or "fable").strip().lower()
    # Any Opus-family token names the one `ask_opus5` namespace — `opus` (the
    # ladder) and every version pin (`opus5`/`opus55`/`opus48`, and their aliased
    # spellings) share it, since there is a single Opus tool.
    canon = oracles.ALIASES.get(requested, requested)
    prefix = OPUS_SESSION_NS if canon.startswith("opus") else ""
    bad_label = _session_label_error(prefix, session)
    if bad_label is not None:  # it would clear the Opus session of that name
        return bad_label
    save = bool(args.get("save", True))
    key = _session_key(prefix, session)
    # Hold the per-session lock: without it a reset can land between an in-flight ask's
    # model await and its record_turn, which then RESURRECTS the just-cleared session. The
    # ask pipeline calls record_turn inside this same lock, so taking it here serializes the
    # two — reset waits for the turn to complete and be recorded, then clears it (so a
    # save=True dump captures that final turn instead of silently dropping it).
    async with _session_lock(key):
        dumped = store.reset(key, save=save)
    return {"status": "ok", "session": session, "cleared": True, "dump": dumped}


async def _handle_stats(args: dict) -> dict:
    """Aggregate the audit JSONL into usage/health buckets — read-only, no model
    call, never cached. The log can be tens of MB across rotation generations, so
    the streaming aggregation runs in a thread to keep the event loop free."""
    window = str(args.get("window") or "24h")
    by = str(args.get("by") or "model")
    model_filter = str(args.get("model") or "").strip() or None
    session_filter = str(args.get("session") or "").strip() or None

    rep = Reporter(f"stats · {by} over {window}")
    try:
        out = await asyncio.to_thread(
            stats.aggregate,
            audit.audit_path(),
            window=window,
            by=by,
            model_filter=model_filter,
            session_filter=session_filter,
        )
    except Exception as exc:  # noqa: BLE001 — stats must never take the server down
        rep.fail(f"aggregation failed: {exc}")
        rep.footer()
        return {"status": "error", "kind": "stats_error", "detail": f"{type(exc).__name__}: {exc}"}
    t = out["totals"]
    avg = f"{t['avg_ms']}ms" if t["avg_ms"] is not None else "n/a"
    shed = f", {t['circuit_open']} shed (circuit open)" if t["circuit_open"] else ""
    rep.ok(f"{t['calls']} calls in window — {t['errors']} errors, avg {avg}{shed}")
    rep.footer()
    return out
