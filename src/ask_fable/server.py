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
from contextvars import ContextVar
from pathlib import Path

import mcp.types as types
from mcp.server import Server
from mcp.shared.exceptions import McpError

from . import (
    atlas,
    audit,
    cache,
    codex,
    config,
    context_store,
    debate_ledger,
    fable,
    gemini,
    grok,
    guard,
    hub,
    kimi,
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
)
from .console import Reporter
from .prompts import (
    ASK_ATLAS_COUNCIL_TOOL_DESCRIPTION,
    ASK_ATLAS_TOOL_DESCRIPTION,
    ASK_CHAIN_TOOL_DESCRIPTION,
    ASK_CODEX_TOOL_DESCRIPTION,
    ASK_COUNCIL_TOOL_DESCRIPTION,
    ASK_DEBATE_TOOL_DESCRIPTION,
    ASK_DEEPSEEK_TOOL_DESCRIPTION,
    ASK_GEMINI_TOOL_DESCRIPTION,
    ASK_GLM_TOOL_DESCRIPTION,
    ASK_GROK_TOOL_DESCRIPTION,
    ASK_KIMI_TOOL_DESCRIPTION,
    ASK_M3_TOOL_DESCRIPTION,
    ASK_OLLAMA_COUNCIL_TOOL_DESCRIPTION,
    ASK_OLLAMA_TOOL_DESCRIPTION,
    ASK_OPENROUTER_COUNCIL_TOOL_DESCRIPTION,
    ASK_OPENROUTER_TOOL_DESCRIPTION,
    ASK_OPUS5_TOOL_DESCRIPTION,
    ASK_TOOL_DESCRIPTION,
    CONFIGURE_ATLAS_COUNCIL_TOOL_DESCRIPTION,
    CONFIGURE_OLLAMA_COUNCIL_TOOL_DESCRIPTION,
    CONFIGURE_OPENROUTER_COUNCIL_TOOL_DESCRIPTION,
    CONFIGURE_TRACING_TOOL_DESCRIPTION,
    CONTEXT_DELETE_TOOL_DESCRIPTION,
    CONTEXT_LIST_TOOL_DESCRIPTION,
    CONTEXT_PACK_TOOL_DESCRIPTION,
    CONTEXT_READ_TOOL_DESCRIPTION,
    CONTEXT_WRITE_TOOL_DESCRIPTION,
    LIST_ATLAS_MODELS_TOOL_DESCRIPTION,
    LIST_OLLAMA_MODELS_TOOL_DESCRIPTION,
    LIST_OPENROUTER_MODELS_TOOL_DESCRIPTION,
    SERVER_INSTRUCTIONS,
    SESSION_LIST_TOOL_DESCRIPTION,
    SESSION_PEEK_TOOL_DESCRIPTION,
    SESSION_STATS_TOOL_DESCRIPTION,
    STATS_TOOL_DESCRIPTION,
    SYNTH_SYSTEM_PROMPT,
    compose_chain_step,
    compose_debate_step,
    compose_synth,
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
    """Hard upper bound on ``ask_council`` wall time (``ASK_FABLE_COUNCIL_TIMEOUT``,
    default ``ASK_FABLE_TIMEOUT + 120``). Bounds the worst case where a backend
    swallows its own inner timeout."""
    return float(_int_env("ASK_FABLE_COUNCIL_TIMEOUT", _int_env("ASK_FABLE_TIMEOUT", 240) + 120))


def _chain_timeout_s(n: int) -> float:
    """Hard upper bound on ``ask_chain`` wall time (``ASK_FABLE_CHAIN_TIMEOUT``,
    default ``max(600, n * ASK_FABLE_TIMEOUT)``). The chain is sequential so the
    default scales with the number of stages."""
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
        "Key(s) of context saved with `context_write` to pull in and prepend "
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


def _q_ctx_props(
    question_desc: str,
    *,
    context_desc: str | None = None,
    context_ref_desc: str | None = None,
    **extra: object,
) -> dict:
    """question + context + context_ref, plus any extra properties."""
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


_ASK_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific question about concrete software code/architecture "
        "(structure, functionality, data flow, module/function relationships, routing).",
        context_ref_desc=(
            "Key(s) of context previously saved with `context_write` to pull in "
            "and prepend to `context` — so you paste a big codebase context ONCE and reference "
            "it by key across many asks instead of re-pasting. Missing keys are reported, not fatal."
        ),
        session={
            "type": "string",
            "default": "default",
            "description": "Conversation key. Reuse it to ask follow-ups (Fable keeps "
            "context); use a new key or reset=true to start a fresh topic.",
        },
        reset={
            "type": "boolean",
            "default": False,
            "description": "Dump+clear this session before asking, starting a fresh conversation.",
        },
        trusted={
            "type": "boolean",
            "default": False,
            "description": "Operator-authorized. When true, the prohibited-use denylist "
            "runs in log-only mode: security vocabulary in the question is audited but "
            "does not block. Use for legitimate security-engineering work (PoC analysis, "
            "CVE research, binary hardening review) where the question genuinely needs "
            "security terms. The operator is responsible for authorizing this flag.",
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

_COUNCIL_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask the selected models; "
        "Fable then synthesizes their answers into one.",
        context_desc=(
            "Optional code snippets, file paths, or structural context (shared by all models)."
        ),
        session=_HUB_SESSION_PROP,
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
            "Key(s) of context saved with `context_write` to pull in and prepend to `context`."
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
            "Key(s) of context saved with `context_write` to pull in and prepend to `context`."
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
            "reasoning_effort, so effort maps to max_tokens + timeout + a prompt nudge.",
        },
    )
)

_OPENROUTER_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask an OpenRouter model.",
        model={
            "type": "string",
            "description": "OpenRouter model id (e.g. 'anthropic/claude-fable-5.1', "
            "'openai/gpt-5.6-sol', 'deepseek/deepseek-v4-pro', 'google/gemini-3.8-flash'). "
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
            "field entirely for non-reasoning models — no wasted probe request.",
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

_ATLAS_COUNCIL_SCHEMA = _tool_schema(
    _q_ctx_props(
        "A specific software/engineering question to ask several Atlas Cloud models; "
        "the adjudicator (GPT-5.6 Sol by default) then synthesizes their answers into one.",
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
            "'deepseek/deepseek-v4-pro']); an 'openrouter:' prefix is optional. Omit to use "
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
            "'deepseek/deepseek-v4-pro']). Call `list_openrouter_models` first.",
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
        "key": {"type": "string", "description": "Key to read back."},
    },
    "required": ["key"],
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
            # Accepts every Opus alias `oracles.ALIASES` registers, so a caller that
            # learned 'opus' from the council/chain/debate docs isn't rejected here.
            "enum": ["fable", "opus5", "opus", "opus-5", "claude-opus-5"],
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
    "ask_opus5": _OPUS_SCHEMA,
    "ask_m3": _M3_SCHEMA,
    "ask_glm": _GLM_SCHEMA,
    "ask_deepseek": _DEEPSEEK_SCHEMA,
    "ask_gemini": _GEMINI_SCHEMA,
    "ask_codex": _CODEX_SCHEMA,
    "ask_grok": _GROK_SCHEMA,
    "ask_kimi": _KIMI_SCHEMA,
    "ask_council": _COUNCIL_SCHEMA,
    "ask_chain": _CHAIN_SCHEMA,
    "ask_debate": _DEBATE_SCHEMA,
    "ask_ollama": _OLLAMA_SCHEMA,
    "ask_ollama_council": _OLLAMA_COUNCIL_SCHEMA,
    "list_ollama_models": _LIST_OLLAMA_SCHEMA,
    "ask_atlas": _ATLAS_SCHEMA,
    "ask_openrouter": _OPENROUTER_SCHEMA,
    "ask_openrouter_council": _OPENROUTER_COUNCIL_SCHEMA,
    "configure_openrouter_council": _CONFIGURE_OPENROUTER_SCHEMA,
    "list_openrouter_models": _LIST_OPENROUTER_SCHEMA,
    "ask_atlas_council": _ATLAS_COUNCIL_SCHEMA,
    "list_atlas_models": _LIST_ATLAS_SCHEMA,
    "configure_ollama_council": _CONFIGURE_OLLAMA_SCHEMA,
    "configure_atlas_council": _CONFIGURE_ATLAS_SCHEMA,
    "configure_tracing": _CONFIGURE_TRACING_SCHEMA,
    "context_write": _CONTEXT_WRITE_SCHEMA,
    "context_pack": _CONTEXT_PACK_SCHEMA,
    "context_read": _CONTEXT_READ_SCHEMA,
    "context_list": _CONTEXT_LIST_SCHEMA,
    "context_delete": _CONTEXT_DELETE_SCHEMA,
    "reset_session": _RESET_SCHEMA,
    "stats": _STATS_SCHEMA,
    "trace_list": _TRACE_LIST_SCHEMA,
    "trace_get": _TRACE_GET_SCHEMA,
    "session_list": _SESSION_LIST_SCHEMA,
    "session_peek": _SESSION_PEEK_SCHEMA,
    "session_stats": _SESSION_STATS_SCHEMA,
}


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
        if kind == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
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


def _guard_check(question: str, context: str, *, trusted: bool = False) -> tuple[bool, str]:
    if trusted:
        allowed, reason = guard.check(question, context, trusted=True)
    else:
        allowed, reason = guard.check(question, context)
    trace_runtime.record_stage("guard", "ok" if allowed else "refused")
    return allowed, reason


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


def _agent_id(server: Server) -> str:
    """Identity of the connecting MCP client (hub attribution).

    Resolution order:
      1. ``ASK_FABLE_AGENT_ID`` (explicit, highest priority — use for per-window names)
      2. MCP InitializeRequest ``clientInfo.name`` (Claude Code / opencode / grok-shell / …)
      3. Harness env hints (``CLAUDECODE``, ``GROK_AGENT``, ``CODEX_*``, …)
      4. Parent process basename (``claude`` → ``claude-code``, ``grok`` → ``grok``, …)
      5. ``"unknown"`` (warn once on stderr)

    Read lazily inside each tool call (init always strictly precedes the first
    tools/call).
    """
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

    Called ONLY from success paths (``ask``, single-oracle, council, chain, debate)
    AFTER the oracle(s) answered — so this can never influence an oracle's answer
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
) -> dict:
    """Deterministic council metadata so a caller can SEE when the council degraded
    or disagreed — not only when fewer models answered than were asked.

    ``recommended_next_action`` consults quorum first for thin panels, then
    material disagreement / consensus, then degradation, so a full-quorum
    divergent panel never claims "safe to act".
    """
    n = len(ok)
    confidence = "high" if n >= 3 else "medium" if n == 2 else "low"
    if n >= 2 and synthesizer is None:
        confidence = "low"  # synthesis failed; this is one raw answer, not a merge
    degraded = n < requested
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
        "degraded": degraded,
        "confidence": confidence,
        "recommended_next_action": nxt,
    }


def build_server() -> Server:
    server: Server = Server("ask_fable", instructions=SERVER_INSTRUCTIONS)
    store = SessionStore()

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name="ask", description=ASK_TOOL_DESCRIPTION, inputSchema=_ASK_SCHEMA),
            types.Tool(
                name="ask_opus5",
                description=ASK_OPUS5_TOOL_DESCRIPTION,
                inputSchema=_OPUS_SCHEMA,
            ),
            types.Tool(name="ask_m3", description=ASK_M3_TOOL_DESCRIPTION, inputSchema=_M3_SCHEMA),
            types.Tool(
                name="ask_glm", description=ASK_GLM_TOOL_DESCRIPTION, inputSchema=_GLM_SCHEMA
            ),
            types.Tool(
                name="ask_deepseek",
                description=ASK_DEEPSEEK_TOOL_DESCRIPTION,
                inputSchema=_DEEPSEEK_SCHEMA,
            ),
            types.Tool(
                name="ask_gemini",
                description=ASK_GEMINI_TOOL_DESCRIPTION,
                inputSchema=_GEMINI_SCHEMA,
            ),
            types.Tool(
                name="ask_codex", description=ASK_CODEX_TOOL_DESCRIPTION, inputSchema=_CODEX_SCHEMA
            ),
            types.Tool(
                name="ask_grok", description=ASK_GROK_TOOL_DESCRIPTION, inputSchema=_GROK_SCHEMA
            ),
            types.Tool(
                name="ask_kimi", description=ASK_KIMI_TOOL_DESCRIPTION, inputSchema=_KIMI_SCHEMA
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
                name="ask_ollama",
                description=ASK_OLLAMA_TOOL_DESCRIPTION,
                inputSchema=_OLLAMA_SCHEMA,
            ),
            types.Tool(
                name="ask_ollama_council",
                description=ASK_OLLAMA_COUNCIL_TOOL_DESCRIPTION,
                inputSchema=_OLLAMA_COUNCIL_SCHEMA,
            ),
            types.Tool(
                name="ask_atlas",
                description=ASK_ATLAS_TOOL_DESCRIPTION,
                inputSchema=_ATLAS_SCHEMA,
            ),
            types.Tool(
                name="ask_atlas_council",
                description=ASK_ATLAS_COUNCIL_TOOL_DESCRIPTION,
                inputSchema=_ATLAS_COUNCIL_SCHEMA,
            ),
            types.Tool(
                name="ask_openrouter",
                description=ASK_OPENROUTER_TOOL_DESCRIPTION,
                inputSchema=_OPENROUTER_SCHEMA,
            ),
            types.Tool(
                name="ask_openrouter_council",
                description=ASK_OPENROUTER_COUNCIL_TOOL_DESCRIPTION,
                inputSchema=_OPENROUTER_COUNCIL_SCHEMA,
            ),
            types.Tool(
                name="configure_openrouter_council",
                description=CONFIGURE_OPENROUTER_COUNCIL_TOOL_DESCRIPTION,
                inputSchema=_CONFIGURE_OPENROUTER_SCHEMA,
            ),
            types.Tool(
                name="list_openrouter_models",
                description=LIST_OPENROUTER_MODELS_TOOL_DESCRIPTION,
                inputSchema=_LIST_OPENROUTER_SCHEMA,
            ),
            types.Tool(
                name="list_atlas_models",
                description=LIST_ATLAS_MODELS_TOOL_DESCRIPTION,
                inputSchema=_LIST_ATLAS_SCHEMA,
            ),
            types.Tool(
                name="list_ollama_models",
                description=LIST_OLLAMA_MODELS_TOOL_DESCRIPTION,
                inputSchema=_LIST_OLLAMA_SCHEMA,
            ),
            types.Tool(
                name="configure_ollama_council",
                description=CONFIGURE_OLLAMA_COUNCIL_TOOL_DESCRIPTION,
                inputSchema=_CONFIGURE_OLLAMA_SCHEMA,
            ),
            types.Tool(
                name="configure_atlas_council",
                description=CONFIGURE_ATLAS_COUNCIL_TOOL_DESCRIPTION,
                inputSchema=_CONFIGURE_ATLAS_SCHEMA,
            ),
            types.Tool(
                name="configure_tracing",
                description=CONFIGURE_TRACING_TOOL_DESCRIPTION,
                inputSchema=_CONFIGURE_TRACING_SCHEMA,
            ),
            types.Tool(
                name="context_write",
                description=CONTEXT_WRITE_TOOL_DESCRIPTION,
                inputSchema=_CONTEXT_WRITE_SCHEMA,
            ),
            types.Tool(
                name="context_pack",
                description=CONTEXT_PACK_TOOL_DESCRIPTION,
                inputSchema=_CONTEXT_PACK_SCHEMA,
            ),
            types.Tool(
                name="context_read",
                description=CONTEXT_READ_TOOL_DESCRIPTION,
                inputSchema=_CONTEXT_READ_SCHEMA,
            ),
            types.Tool(
                name="context_list",
                description=CONTEXT_LIST_TOOL_DESCRIPTION,
                inputSchema=_CONTEXT_LIST_SCHEMA,
            ),
            types.Tool(
                name="context_delete",
                description=CONTEXT_DELETE_TOOL_DESCRIPTION,
                inputSchema=_CONTEXT_DELETE_SCHEMA,
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

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        _CALL_AGENT_ID.set(_agent_id(server))
        try:
            request_id = str(server.request_context.request_id)
        except LookupError:
            request_id = None
        with trace_runtime.tool_trace(name, arguments or {}, mcp_request_id=request_id):
            try:
                validation_error = _schema_error(name, arguments or {})
                if validation_error is not None:
                    return [
                        _text({"status": "error", "kind": "bad_args", "detail": validation_error})
                    ]
                if name == "ask":
                    return [_text(await _handle_ask(store, arguments or {}))]
                if name == "ask_opus5":
                    return [_text(await _handle_opus5(store, arguments or {}))]
                if name == "ask_m3":
                    return [_text(await _handle_m3(arguments or {}))]
                if name == "ask_glm":
                    return [_text(await _handle_glm(arguments or {}))]
                if name == "ask_deepseek":
                    return [_text(await _handle_deepseek(arguments or {}))]
                if name == "ask_gemini":
                    return [_text(await _handle_gemini(arguments or {}))]
                if name == "ask_codex":
                    return [_text(await _handle_codex(arguments or {}))]
                if name == "ask_grok":
                    return [_text(await _handle_grok(arguments or {}))]
                if name == "ask_kimi":
                    return [_text(await _handle_kimi(arguments or {}))]
                if name == "ask_council":
                    return [_text(await _handle_council(arguments or {}))]
                if name == "ask_chain":
                    return [_text(await _handle_chain(arguments or {}))]
                if name == "ask_debate":
                    return [_text(await _handle_debate(arguments or {}))]
                if name == "ask_ollama":
                    return [_text(await _handle_ollama(arguments or {}))]
                if name == "ask_ollama_council":
                    return [_text(await _handle_ollama_council(arguments or {}))]
                if name == "ask_atlas_council":
                    return [_text(await _handle_atlas_council(arguments or {}))]
                if name == "ask_atlas":
                    return [_text(await _handle_atlas(arguments or {}))]
                if name == "ask_openrouter_council":
                    return [_text(await _handle_openrouter_council(arguments or {}))]
                if name == "configure_openrouter_council":
                    return [_text(_handle_configure_openrouter(arguments or {}))]
                if name == "ask_openrouter":
                    return [_text(await _handle_openrouter(arguments or {}))]
                if name == "list_openrouter_models":
                    listing = await _handle_list_openrouter(arguments or {})
                    task = str((arguments or {}).get("task") or "").strip()
                    if task and bool((arguments or {}).get("interactive", True)):
                        listing["selection"] = await _elicit_atlas_selection(
                            server, listing, task
                        )
                    return [_text(listing)]
                if name == "list_atlas_models":
                    listing = await _handle_list_atlas(arguments or {})
                    task = str((arguments or {}).get("task") or "").strip()
                    if task and bool((arguments or {}).get("interactive", True)):
                        listing["selection"] = await _elicit_atlas_selection(server, listing, task)
                    return [_text(listing)]
                if name == "list_ollama_models":
                    return [_text(await _handle_list_ollama(arguments or {}))]
                if name == "configure_ollama_council":
                    return [_text(_handle_configure_ollama(arguments or {}))]
                if name == "configure_atlas_council":
                    return [_text(_handle_configure_atlas(arguments or {}))]
                if name == "configure_tracing":
                    return [_text(_handle_configure_tracing(arguments or {}))]
                if name == "context_write":
                    return [_text(_handle_context_write(arguments or {}))]
                if name == "context_pack":
                    return [_text(_handle_context_pack(arguments or {}))]
                if name == "context_read":
                    return [_text(_handle_context_read(arguments or {}))]
                if name == "context_list":
                    return [_text(_handle_context_list(arguments or {}))]
                if name == "context_delete":
                    return [_text(_handle_context_delete(arguments or {}))]
                if name == "reset_session":
                    return [_text(_handle_reset(store, arguments or {}))]
                if name == "stats":
                    return [_text(await _handle_stats(arguments or {}))]
                if name == "trace_list":
                    args = arguments or {}
                    return [
                        _text(
                            trace_query.list_traces(
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
                            trace_query.get_trace(
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
    """`ask` — the multi-turn Fable tool."""
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
    """`ask_opus5` — the multi-turn Claude Opus 5 tool.

    Identical contract to `ask`; sessions are namespaced so the same key on both
    tools is two independent conversations (an SDK session id is bound to the
    model that created it, so resuming a Fable thread on Opus would silently
    swap models mid-conversation)."""
    return await _handle_claude_ask(
        store,
        args,
        tool="ask_opus5",
        bridge=opus,
        display="Opus 5",
        model=opus.OPUS_MODEL,
        oracle_key="opus",
        session_prefix=OPUS_SESSION_NS,
    )


# Session-key namespace for `ask_opus5`, so its sessions can't collide with (or
# resume) `ask`'s Fable threads. `reset_session(model='opus5')` applies the same
# prefix, which is why it lives at module scope.
OPUS_SESSION_NS = "opus5:"


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
    """Shared multi-turn Anthropic handler — the implementation behind `ask`
    (Fable) and `ask_opus5` (Claude Opus 5).

    Both run the identical pipeline (reset → resolve refs → guard → run the
    bridge → refused/error/ok branches → sidecar → session record → save); only
    the tool name, bridge module, label, and session namespace differ.
    ``bridge`` is a module exposing ``run(question, context, *, resume, on_think)``
    — resolved at call time so tests can monkeypatch ``server.fable.run``."""
    question = str(args.get("question") or "")
    session = str(args.get("session") or "default")
    key = session_prefix + session  # store/audit/hub key; `session` is what callers see
    reset = bool(args.get("reset") or False)
    trusted = bool(args.get("trusted") or False)

    rep = Reporter(f"{tool} · {display}")
    rep.info("question", _preview(question))
    if trusted:
        rep.info("trusted session", session)

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
        return {"status": "refused", "stage": "guard", "reason": reason}
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
    trace_runtime.record_provider(res.telemetry, res.status, res.thinking, kind=res.kind)
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
        return {"status": "refused", "stage": "model", "reason": res.text}

    if res.status == "error":
        rep.fail(f"{display} error ({res.kind}): {res.text}", secs)
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
            outcome_detail=res.text,
        )
        # `model` so the failure buckets under the oracle that failed: without it
        # stats(by="model") drops every errored call — including a breaker-shed
        # one — as "?", which is precisely the backend the operator is looking for.
        return {"status": "error", "kind": res.kind, "detail": res.text, "model": model}

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
) -> dict:
    """Shared single-oracle handler — the unified implementation behind
    ``ask_m3`` / ``ask_gemini`` / ``ask_codex`` / ``ask_grok`` / ``ask_glm`` /
    ``ask_deepseek`` / ``ask_ollama`` / ``ask_atlas``.

    Each of those tools runs the same pipeline (resolve refs → guard → cache
    lookup → run oracle → refused/error/ok branches → sidecar → save → payload);
    only the tool name, oracle key, model label, and audit session differ.
    ``effort`` is atlas answer-budget / grok reasoning-effort; other oracles ignore it.
    For ``grok``, ``model`` is also passed through to the bridge (env default otherwise)."""
    question = str(args.get("question") or "")

    rep = Reporter(f"{tool} · {display}")
    rep.info("question", _preview(question))

    context, ref_resolved, ref_missing, ref_fail = _prepare_context(args)
    _report_refs(rep, ref_resolved, ref_missing)
    if ref_fail is not None:
        rep.fail("no context resolved (all context_ref keys missing)")
        rep.footer()
        return ref_fail

    allowed, reason = _guard_check(question, context)
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
        )
        return {"status": "refused", "stage": "guard", "reason": reason}
    rep.ok("guard passed")

    ck, served = _cache_lookup(tool, [model], question, context, effort=effort)
    if served is not None:
        rep.ok(f"cache hit ({served['cache_age_s']}s old)")
        rep.footer()
        return served

    rep.start(f"asking {display} ({model})")
    t0 = time.monotonic()
    # Named CLI bridges (grok) honor an explicit model override; dynamic tokens
    # (atlas:/ollama:) carry the model in the key itself.
    bridge_model = model if oracle_key in ("grok", "kimi") else None
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
        return {"status": "refused", "stage": "model", "reason": res.text}

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
        return {"status": "error", "kind": res.kind, "detail": res.text, "model": model}

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
    _add_refs(payload, ref_resolved, ref_missing)
    _add_thinking(payload, res.thinking)
    fu = _followup(sc, context)
    if fu:
        payload["followup"] = fu
    if saved:
        payload["saved"] = saved
    cache.put(ck, trace_runtime.prepare_cache_store(payload))
    return payload


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
    binary is installed. Reports binary_missing when the CLI isn't on PATH."""
    return await _handle_single(
        args,
        tool="ask_grok",
        oracle_key="grok",
        session="grok",
        display="Grok",
        model=grok.grok_model(),
    )


async def _handle_kimi(args: dict) -> dict:
    """Ask Moonshot Kimi (kimi-code/k3) on its own via the local `kimi` CLI —
    guarded, single-turn. Prefer this over ``ask_atlas`` with ``moonshotai/kimi-*``
    when the binary is installed: it runs on the operator's Kimi Code subscription
    rather than per-token Atlas billing, and k3 carries 1M context against Atlas's
    262k. Reports binary_missing when the CLI isn't on PATH."""
    return await _handle_single(
        args,
        tool="ask_kimi",
        oracle_key="kimi",
        session="kimi",
        display="Kimi",
        model=oracles.label("kimi"),
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
    if grok.looks_like_grok_model(model) and grok.available():
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
            tok = oracles.OPENROUTER_PREFIX + tok[len(oracles.OPENROUTER_PREFIX):]
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
            or (kimi.available()
                and kimi.local_alias_for(oracles.openrouter_model(t) or "") is not None)
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
        synth_tok, synth_err = _resolve_synth_token(synth_raw)
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
        question, context, selected, [], "ask_openrouter_council",
        ref_resolved, ref_missing, session=session,
        synthesizer=synth_tok, synth_note=note,
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
        tok, err = _resolve_synth_token(str(args["synthesizer"]))
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
    if grok.looks_like_grok_model(model) and grok.available():
        local_model = model.split("/", 1)[-1] if "/" in model else model
        return await _handle_single(
            args, tool="ask_grok", oracle_key="grok", session="grok",
            display="Grok", model=local_model, effort=effort,
        )
    if kimi.available() and kimi.local_alias_for(model) is not None:
        return await _handle_single(
            args, tool="ask_kimi", oracle_key="kimi", session="kimi",
            display="Kimi", model=model, effort=effort,
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
    cat = await asyncio.to_thread(
        openrouter.catalog, 8.0, task=task, recommendation_limit=limit
    )
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


async def _elicit_atlas_selection(server: Server, listing: dict, task: str) -> dict:
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
    effort_choices = listing.get("effort_choices") or atlas.effort_menu()
    effort_ids = [item["value"] for item in effort_choices]
    schema = {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "title": "Atlas model",
                "description": "Task-ranked choices; cost and fit are shown in each label.",
                "enum": model_ids,
                "enumNames": [
                    f"{item['label']} — {item['picker_description']}" for item in recommendations
                ],
                "default": model_ids[0],
            },
            "effort": {
                "type": "string",
                "title": "Reasoning effort",
                "enum": effort_ids,
                "enumNames": [item["label"] for item in effort_choices],
                "default": atlas.default_effort(),
            },
        },
        "required": ["model", "effort"],
    }
    try:
        response = await session.elicit_form(
            f"Choose an Atlas model and effort for: {task}",
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
    cat = atlas.catalog(task=task, recommendation_limit=limit)
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


def _source(res) -> dict:
    """Public per-oracle record for the council payload (no reasoning trace).
    Panelists now emit a sidecar (shared oracle prompt); strip it so raw JSON blocks
    don't leak into the merged answer or the ``sources`` view, but surface each
    panelist's ``recommendation`` as attribution data (who endorsed what)."""
    d: dict = {"status": res.status, "model": res.model}
    if res.status == "ok":
        prose, sc = sidecar.extract(res.text)
        d["answer"] = prose
        if sc and sc.get("recommendation"):
            d["recommendation"] = sc["recommendation"]
    elif res.status == "refused":
        d["reason"] = res.text
    else:
        d["kind"], d["detail"] = res.kind, res.text
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
    recs: list[str], panel_size: int | None = None, confs: list[str] | None = None
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

    ``material_disagreement`` = at least one voter pushes to act (``apply``) while
    another opposes/gates it (``reject``/``investigate``). ``consensus``: 'strong' (all
    voters agreed, all panelists voted, not all-low), 'divergent' (materially opposed),
    'partial' (mixed, incomplete-coverage, or hedged-low), 'unknown' (fewer than two
    panelists answered at all)."""
    if panel_size is None:
        panel_size = len(recs)
    distinct = set(recs)
    material = bool(distinct & _ACT_RECS) and bool(distinct & _BLOCK_RECS)
    if material:
        return "divergent", True
    if len(recs) < 2:
        # <2 usable recommendations. A coverage gap (≥2 answered but a sidecar dropped
        # a voter) is 'partial'; only a genuinely thin panel (<2 answered) is 'unknown'.
        return ("partial", False) if (panel_size >= 2 and recs) else ("unknown", False)
    if len(distinct) == 1 and len(recs) == panel_size:
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


def _resolve_synth_token(raw: str) -> tuple[str | None, str | None]:
    """Resolve a user-supplied synthesizer name to an oracle token.

    Accepts everything a council member list accepts — KNOWN names, aliases
    ('gpt' → codex), 'ollama:<model>' and 'atlas:<model-id>' tokens — plus a bare
    Atlas model id like 'openai/gpt-5.6-sol' (an id with a '/' is retried with the
    'atlas:' prefix). Atlas id casing is preserved (Atlas ids are case-sensitive).
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
    if "/" in tok and not tok.lower().startswith(oracles.ATLAS_PREFIX):
        recognized, _ = oracles.resolve_ordered([oracles.ATLAS_PREFIX + tok])
        if recognized:
            return recognized[0], None
    return None, (
        f"unknown synthesizer: {raw!r} — use one of {', '.join(oracles.KNOWN)}, an alias "
        f"({', '.join(sorted(oracles.ALIASES))}), or an 'ollama:<model>'/'atlas:<model-id>' token"
    )


async def _handle_council(args: dict) -> dict:
    """Fan a question out to the selected models, then synthesize the answers.

    Single-turn (no session/resume). ``models`` (default ['fable','minimax'],
    plus 'deepseek' when its API key is configured — cheap-first) picks from
    ['fable','deepseek','minimax','glm','gemini','codex'] plus any 'ollama:<model>'
    token. ``synthesizer`` picks the model that reconciles the panel (default
    Fable; falls back to Fable when it is unavailable or fails). Returns the
    merged ``answer`` plus each oracle's raw answer under ``sources``; degrades to
    the lone answerer when only one responds, and refuses/errors only when none do.
    """
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
    )


async def _handle_ollama_council(args: dict) -> dict:
    """Fan a question out to several Ollama Cloud models; Fable synthesizes them.

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
    session = str(args.get("session") or "").strip() or None
    trace_runtime.set_orchestration(mode="council", models=list(selected), flavor="ollama")
    return await _council(
        question,
        context,
        selected,
        [],
        "ask_ollama_council",
        ref_resolved,
        ref_missing,
        session=session,
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
            tok = oracles.ATLAS_PREFIX + tok[len(oracles.ATLAS_PREFIX):]
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
    )


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
        return {
            "status": "error",
            "kind": "no_models",
            "detail": "no recognized models in the pipeline",
        }

    allowed, reason = _guard_check(question, context)
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
        return {"status": "refused", "stage": "guard", "reason": reason}
    rep.ok("guard passed")

    # Order-sensitive cache key: join the pipeline into ONE token so cache.key's internal
    # sort can't reorder it — 'm3 > glm > fable' must not collide with 'glm > m3 > fable'.
    ck, served = _cache_lookup(title, [" > ".join(selected)], question, context)
    if served is not None:
        rep.ok(f"cache hit ({served['cache_age_s']}s old)")
        rep.footer()
        return served

    t0 = time.monotonic()
    n = len(selected)
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
        return {
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
            "requested": n,
        }

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
            return {"status": "refused", "stage": "model", "reason": refused["res"].text}
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
        return {"status": "error", "kind": err["res"].kind, "detail": err["res"].text}

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
                compose_synth(question, labelled),
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
            synth.telemetry, synth.status, synth.thinking, kind=synth.kind
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
    rep.footer(f"done — {len(ok_steps)}/{n} stages answered")
    trace_runtime.set_orchestration(
        quorum=f"{len(ok_steps)}/{n}",
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
        quorum=f"{len(ok_steps)}/{n}",
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
                "recommendation": (s["sidecar"] or {}).get("recommendation"),
                "confidence": (s["sidecar"] or {}).get("confidence"),
            }
            for s in steps
        ],
        "recommendation_drift": trail,
        "material_drift": material,
        "answered": len(ok_steps),
        "requested": n,
        "saved": saved,
    }
    if fallback:
        payload["fallback"] = fallback
    _add_refs(payload, ref_resolved, ref_missing)
    _add_thinking(payload, head_thinking)
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
) -> dict:
    ref_resolved, ref_missing = ref_resolved or [], ref_missing or []
    hub_session = (session or "").strip() or "ask_debate"
    title = "ask_debate"
    if proposer is None or opponent is None:
        miss = "proposer" if proposer is None else "opponent"
        return {"status": "error", "kind": "no_models", "detail": f"unrecognized {miss} model"}

    rep = Reporter(f"{title} · {oracles.label(proposer)} vs {oracles.label(opponent)}")
    rep.info("question", _preview(question))

    allowed, reason = _guard_check(question, context)
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
        return {"status": "refused", "stage": "guard", "reason": reason}
    rep.ok("guard passed")

    # The adjudicator is part of the identity of the debate — a Fable ruling and an
    # Opus ruling on the same pair are different answers, so it joins the cache key.
    # Left out when it's the default, so existing cached debates still hit.
    stamp = f"{proposer} vs {opponent} r{rounds}"
    if adjudicator != oracles.SYNTHESIZER:
        stamp += f" adj:{adjudicator}"
    ck, served = _cache_lookup(title, [stamp], question, context)
    if served is not None:
        rep.ok(f"cache hit ({served['cache_age_s']}s old)")
        rep.footer()
        return served

    t0 = time.monotonic()
    turns: list[dict] = []

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
        if resolution == "stalemate" and sc:
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
            synth_fallback=resolution.startswith("degraded"),
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
            synth_fallback=resolution.startswith("degraded"),
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
            "material_disagreement": resolution in ("stalemate", "adjudicated"),
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
                    "recommendation": (t["sc"] or {}).get("recommendation"),
                    "confidence": (t["sc"] or {}).get("confidence"),
                }
                for t in turns
            ],
            "saved": saved,
        }
        _add_refs(payload, ref_resolved, ref_missing)
        _add_thinking(payload, head_thinking)
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
        # Adjudicator unavailable — use the revised answer if present, else the proposal.
        best = v or p
        f2 = {**(flags or {}), "adjudication_failed": jt["res"].kind or jt["res"].status}
        return finish(
            best["prose"],
            best["sc"],
            resolution,
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
                    return {"status": "refused", "stage": "model", "reason": p["res"].text}
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
        # Degrade to the best answer produced so far rather than failing outright.
        ok_turns = [t for t in turns if t["res"].status == "ok"]
        if ok_turns:
            best = ok_turns[-1]
            return finish(
                best["prose"],
                best["sc"],
                "degraded_timeout",
                answered_by=best["model"],
                head_thinking=best["res"].thinking,
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
) -> dict:
    ref_resolved, ref_missing = ref_resolved or [], ref_missing or []
    synth_key = synthesizer or oracles.SYNTHESIZER
    hub_session = (session or "").strip() or title
    rep = Reporter(title + " · " + " + ".join(oracles.label(k) for k in selected))
    rep.info("question", _preview(question))
    if unknown:
        rep.warn(f"ignoring unknown models: {', '.join(unknown)}")

    allowed, reason = _guard_check(question, context)
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
        return {"status": "refused", "stage": "guard", "reason": reason}
    rep.ok("guard passed")

    # A non-default synthesizer produces a different merged answer, so it joins
    # the cache key; the default key stays byte-identical for cache continuity.
    cache_models = (
        selected if synth_key == oracles.SYNTHESIZER else [*selected, f"synth:{synth_key}"]
    )
    ck, served = _cache_lookup(title, cache_models, question, context)
    if served is not None:
        rep.ok(f"cache hit ({served['cache_age_s']}s old)")
        rep.footer()
        return served

    rep.start("asking " + ", ".join(oracles.label(k) for k in selected) + " in parallel")
    t0 = time.monotonic()
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
    council_timeout = _council_timeout_s()
    sem = asyncio.Semaphore(_max_parallel())

    async def _bounded(k: str):
        return await _timed(oracles.run_bounded(sem, k, question, context))

    tasks = [asyncio.create_task(_bounded(k), name=k) for k in selected]
    _, pending = await asyncio.wait(tasks, timeout=council_timeout)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)  # let cancellation settle

    # Harvest results in ``selected`` order, attributing every outcome to its real
    # oracle key (a timed-out or crashed bridge must still show up as ITS OWN source,
    # not collapse into a shared placeholder).
    timed: list[tuple] = []
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
            return {
                "status": "refused",
                "stage": "model",
                "reason": refused.text,
                "sources": sources,
            }
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
        return {"status": "error", "kind": err.kind, "detail": err.text, "sources": sources}

    # Exactly one answered — return it directly, no synthesis needed.
    if len(ok) == 1:
        lone = ok[0]
        lone_answer = sidecar.extract(lone.text)[0]  # drop the panelist's sidecar block
        rep.footer(f"one oracle answered ({lone.model}); others unavailable")
        trace_runtime.set_orchestration(
            quorum=f"1/{len(selected)}",
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
            quorum=f"1/{len(selected)}",
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
            **_council_envelope(ok, len(selected), None, consensus="unknown"),
        }
        _add_refs(payload, ref_resolved, ref_missing)
        _add_thinking(payload, lone.thinking)
        cache.put(ck, trace_runtime.prepare_cache_store(payload))
        return payload

    # Two or more answered — the synthesizer (Fable unless overridden) reconciles
    # them into one. Extract each panelist's (prose, sidecar) once; use
    # recommendations for a deterministic consensus signal and anonymize+reorder
    # for the synthesis.
    extracted = [(r, sidecar.extract(r.text)) for r in ok]
    recs = [sc["recommendation"] for _, (_, sc) in extracted if sc and sc.get("recommendation")]
    confs = [sc.get("confidence") for _, (_, sc) in extracted if sc and sc.get("recommendation")]
    consensus, material = _consensus(recs, len(ok), confs)
    votes = _vote_vector(recs, len(ok))

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
        return compose_synth(question, labelled, material_disagreement=material)

    synth_used: str | None = synth_key
    synth_fallback: str | None = None
    if synth_key != oracles.SYNTHESIZER and not oracles.available(synth_key):
        rep.warn(f"synthesizer {oracles.label(synth_key)} not available — falling back to Fable")
        synth_used, synth_fallback = oracles.SYNTHESIZER, "fable"
    rep.start(f"synthesizing with {oracles.label(synth_used)}")
    if material:
        rep.warn("panelists gave conflicting recommendations — forcing adjudication")

    async def _synthesize(key: str):
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
        trace_runtime.record_provider(res.telemetry, res.status, res.thinking, kind=res.kind)
        trace_runtime.record_stage(
            "orchestration.synthesis",
            res.status,
            kind=trace_runtime.EventKind.ORCHESTRATION,
            orchestration={"mode": "council", "role": "synthesizer", "provider": oracles.label(key)},
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

    audit.record(
        decision="allowed",
        stage=None,
        reason="council_synth" if synth_label else "council_synth_fallback",
        question=question,
        context=context,
        session="council",
        model="council",
        duration_ms=duration_ms(),
        quorum=f"{len(ok)}/{len(selected)}",
        consensus=consensus,
        synth_fallback=synth_label is None,
    )
    trace_runtime.set_orchestration(
        quorum=f"{len(ok)}/{len(selected)}",
        consensus=consensus,
        synthesizer=synth_key,
        synth_fallback=synth_label is None,
    )
    answered_by = synth_label or ok[0].model
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
            len(selected),
            synth_label,
            consensus=consensus,
            material_disagreement=material,
        ),
    }
    _add_refs(payload, ref_resolved, ref_missing)
    _add_thinking(payload, synth.thinking if synth_label else ok[0].thinking)
    cache.put(ck, trace_runtime.prepare_cache_store(payload))
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


def _resolve_context(context: str, context_ref) -> tuple[str, list[str], list[str]]:
    """Merge stored blobs referenced by ``context_ref`` (a key or list of keys) with
    the inline ``context``. Returns (effective_context, resolved_keys, missing_keys).
    Stored blobs come first, each labelled, then the inline context."""
    if isinstance(context_ref, str):
        keys = [context_ref.strip()] if context_ref.strip() else []
    elif isinstance(context_ref, list):
        keys = [str(k).strip() for k in context_ref if str(k).strip()]
    else:
        keys = []
    resolved: list[str] = []
    missing: list[str] = []
    parts: list[str] = []
    for k in keys:
        val = context_store.get(k)
        if val is None:
            missing.append(k)
        else:
            resolved.append(k)
            parts.append(f"[context:{k}]\n{val}")
    if (context or "").strip():
        parts.append(context.strip())
    return "\n\n".join(parts), resolved, missing


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

    Returns (effective_context, resolved, missing, fail). ``fail`` is a
    ``needs_context`` result ONLY when refs were requested but every one missed AND
    no other context remains (no inline context, no resolved refs, no session
    history) — the one case where the model would answer blind against the agent's
    stated intent. Every other combination proceeds (missing keys are reported, not
    fatal), identically for ask / single-model / councils."""
    eff, resolved, missing = _resolve_context(
        str(args.get("context") or ""), args.get("context_ref")
    )
    fail = None
    if missing and not (eff.strip() or has_history):
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
        "paste these into `context` (or `context_write` them and pass `context_ref`)"
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
    key = str(args.get("key") or "").strip()
    if not key:
        return {"status": "error", "kind": "bad_args", "detail": "`key` is required"}
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


def _handle_reset(store: SessionStore, args: dict) -> dict:
    session = str(args.get("session") or "default")
    # `ask_opus5` namespaces its sessions, so clearing one needs the same prefix.
    # Resolve through ALIASES: an agent that learned 'opus' from the council docs
    # would otherwise silently clear the FABLE session instead of the Opus one.
    requested = str(args.get("model") or "fable").strip().lower()
    prefix = OPUS_SESSION_NS if oracles.ALIASES.get(requested, requested) == "opus" else ""
    save = bool(args.get("save", True))
    dumped = store.reset(prefix + session, save=save)
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
