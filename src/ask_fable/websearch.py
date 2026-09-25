"""Opt-in web-search / OSINT research router — the one tool that browses.

Every other ``ask_*`` oracle in ask_fable is deliberately TOOLLESS: the model gets
pure text and cannot browse, open files, or fetch anything. ``ask_websearch`` is
the single, walled-off exception. It does NOT add a new transport — it reuses the
existing ``grok``, ``agy`` (Gemini) and Claude (``fable``) bridges, flipping their
``web_search`` flag on and handing them a research/OSINT system prompt. The caller
selects which model runs the search with ``model``.

All backends here are flat-plan / OAuth sources, so a search costs nothing beyond
the subscription: ``grok`` drives the local ``grok`` CLI's live search; the Claude
keys use native ``WebSearch``/``WebFetch`` over Claude Code's OAuth session;
``gemini`` drives the local ``agy`` CLI's ``search_web`` tool.

Backends differ in how well the "search, don't do anything else" boundary is
enforced. On ``grok`` and Claude it is a real gate — the CLI's disallowed-tool
list / the SDK's ``allowed_tools`` keep Bash/Read/Write off. ``agy`` has no such
flag and keeps its own headless permission policy instead: ``search_web`` is
allowed, page fetch (``read_url``) is auto-denied unless the operator adds an
allow-rule to agy's settings.json — and a denial aborts the turn with empty
output. So the ``gemini`` backend gets a research prompt that additionally tells
it to stay on ``search_web`` (see :mod:`ask_fable.gemini`).

Off by default — the ``ask_websearch`` handler in ``server.py`` gates on
``ASK_FABLE_ALLOW_WEBSEARCH`` before this router is ever reached.
"""

from __future__ import annotations

import os

from . import anthropic_variants, config, fable, gemini, grok, opus
from .fable import ClaudeSpec
from .oracle_common import OracleResult
from .prompts import WEBSEARCH_SYSTEM_PROMPT, WEBSEARCH_SYSTEM_PROMPT_AGY

DEFAULT_WEBSEARCH_MODEL = "grok"

# Canonical model keys this router can drive. ``grok`` -> local grok CLI live
# search; the rest -> Claude native WebSearch/WebFetch over the OAuth session.
# Same-lab Claude variants are fine here: this is a router, not a council, so the
# independence rules that keep opus48/sonnet out of council tiers don't apply.
_ALLOWED = ("grok", "gemini", "sonnet", "opus48", "opus5", "fable")

# Operator-facing spellings -> canonical key.
_ALIASES = {
    "agy": "gemini",
    "gemini-3.1-pro": "gemini",
    "gemini-3.1-pro-high": "gemini",
    "gemini3.1pro": "gemini",
    "opus": "opus5",
    "opus-5": "opus5",
    "claude-opus-5": "opus5",
    "opus4.8": "opus48",
    "opus-4.8": "opus48",
    "claude-opus-4-8": "opus48",
    "sonnet5": "sonnet",
    "sonnet-5": "sonnet",
    "claude-sonnet-5": "sonnet",
    "fable51": "fable",  # the `fable` ladder already tracks 5.1
    "fable5.1": "fable",
    "grok-4.6": "grok",
}


def websearch_model() -> str:
    """Default model when the caller names none (config file -> env -> grok)."""
    return (
        config.get_str("websearch_model")
        or (os.environ.get("ASK_FABLE_WEBSEARCH_MODEL") or "").strip()
        or DEFAULT_WEBSEARCH_MODEL
    )


def resolve(model: str | None) -> str | None:
    """Normalize a selector to a canonical key in :data:`_ALLOWED`, or None.

    An empty selector resolves to the configured default; an unknown one returns
    None so the caller can report a clean error listing the allowed models."""
    m = (model or "").strip().lower()
    if not m:
        m = websearch_model().strip().lower()
    m = _ALIASES.get(m, m)
    return m if m in _ALLOWED else None


def _claude_spec(key: str) -> ClaudeSpec:
    """The ClaudeSpec for a Claude key (opus5 / opus48 / sonnet / fable)."""
    if key == "opus5":
        # Matches the `ask_opus5` tool: the newest Opus, resolved through the ladder.
        return opus.opus_spec()
    if key == "fable":
        return fable.fable_spec()
    return anthropic_variants.SPECS[key]  # opus48 / sonnet


async def run(
    question: str,
    context: str = "",
    *,
    model: str | None = None,
    timeout: float | None = None,
    effort: str | None = None,
) -> OracleResult:
    """Route one research turn to the selected search-capable backend.

    Never raises for expected failures — the underlying bridge returns an error
    ``OracleResult`` (e.g. ``binary_missing`` for grok). An unknown model is a
    clean ``unsupported_model`` error rather than a dispatch crash."""
    key = resolve(model)
    if key is None:
        return OracleResult(
            "error",
            kind="unsupported_model",
            text=f"ask_websearch model must be one of {list(_ALLOWED)} (got {model!r})",
        )
    if key == "grok":
        return await grok.run(
            question,
            context,
            timeout=timeout,
            effort=effort,
            web_search=True,
            system_prompt=WEBSEARCH_SYSTEM_PROMPT,
        )
    if key == "gemini":
        # agy's own headless permission policy, not a prompt choice: search_web is
        # allowed, read_url is auto-denied and a denial aborts the turn with empty
        # stdout. So this backend gets the research contract plus a search-only note.
        return await gemini.run(
            question,
            context,
            timeout=timeout,
            web_search=True,
            system_prompt=WEBSEARCH_SYSTEM_PROMPT_AGY,
        )
    return await fable.run(
        question,
        context,
        timeout=timeout,
        spec=_claude_spec(key),
        web_search=True,
        system_prompt=WEBSEARCH_SYSTEM_PROMPT,
    )
