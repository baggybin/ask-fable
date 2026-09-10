"""Invoke Claude Opus 5 (claude-opus-5) for a single reasoning turn.

The second first-class Anthropic oracle next to Fable, and a drop-in swap for it
in every mode: the `ask_opus5` tool mirrors `ask` (multi-turn sessions), and the
`opus` token works anywhere `fable` does — council member, council synthesizer,
chain stage, debate proposer/opponent/adjudicator.

There is no separate transport: this is a thin wrapper over ``fable.run`` with a
different :class:`~ask_fable.fable.ClaudeSpec`, so Opus 5 rides the exact same
Claude Agent SDK path (Claude Code's OAuth session, tools disabled, resumable
sessions) with the CLI (``claude -p --model claude-opus-5``) as the same
fallback. Only the model id, oracle key, and label differ.

Why both: Fable is the most capable model but the priciest (~$10/$50 per MTok);
Opus 5 is half that (~$5/$25) and faster, so it is the better default for
high-volume reasoning and for panels that want a second strong Anthropic voice
without doubling the Fable bill.
"""

from __future__ import annotations

from collections.abc import Callable

from . import fable
from .oracle_common import OracleResult

OPUS_MODEL = "claude-opus-5"
OPUS = fable.ClaudeSpec(model=OPUS_MODEL, key="opus", label="Opus 5")


async def run(
    question: str,
    context: str = "",
    *,
    resume: str | None = None,
    timeout: float | None = None,
    use_cli: bool | None = None,
    system_prompt: str | None = None,
    on_think: Callable[[str], None] | None = None,
) -> OracleResult:
    """Run one Opus 5 turn — same contract as ``fable.run`` (never raises for
    expected failures; ``resume`` continues a prior SDK session; ``on_think``
    streams reasoning blocks live on the SDK path)."""
    return await fable.run(
        question,
        context,
        resume=resume,
        timeout=timeout,
        use_cli=use_cli,
        system_prompt=system_prompt,
        on_think=on_think,
        spec=OPUS,
    )
