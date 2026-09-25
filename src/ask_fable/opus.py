"""Invoke Claude Opus — the newest available — for a single reasoning turn.

The second first-class Anthropic oracle next to Fable, and a drop-in swap for it
in every mode: the `ask_opus5` tool mirrors `ask` (multi-turn sessions), and the
`opus` token works anywhere `fable` does — council member, council synthesizer,
chain stage, debate proposer/opponent/adjudicator.

Like `fable`, the model id is NOT pinned: ``opus`` is a
:class:`~ask_fable.fable.Ladder` that walks ``OPUS_CANDIDATES`` newest-first
(5.5, then 5) and asks for the best one the local Claude Code build hasn't
rejected, so `opus` tracks the newest Opus instead of whichever id was current
when this line was written. A too-old build demotes 5.5 once and answers as
Opus 5 rather than failing. ``ASK_FABLE_OPUS_MODEL`` pins an exact id; the
``opus55`` / ``opus5`` / ``opus48`` tokens (in ``anthropic_variants``) NAME a
specific version and never ladder.

There is no separate transport: this is a thin wrapper over ``fable.run`` with
the ``opus`` ladder, so Opus rides the exact same Claude Agent SDK path (Claude
Code's OAuth session, tools disabled, resumable sessions) with the CLI as the
same fallback. Only the model family, oracle key, and label differ.

Why both: Fable is the most capable model but the priciest (~$10/$50 per MTok);
Opus is roughly half that and faster, so it is the better default for
high-volume reasoning and for panels that want a second strong Anthropic voice
without doubling the Fable bill.
"""

from __future__ import annotations

from collections.abc import Callable

from . import fable
from .oracle_common import OracleResult

OPUS_PREFERRED_MODEL = "claude-opus-5-5"
OPUS_MODEL = "claude-opus-5"  # the floor: always served, always accepted
# Opus ids newest-first, mirroring ``fable.FABLE_CANDIDATES``.
OPUS_CANDIDATES: tuple[str, ...] = (OPUS_PREFERRED_MODEL, OPUS_MODEL)
OPUS_MODEL_ENV = "ASK_FABLE_OPUS_MODEL"  # pin an exact id, skipping the ladder

OPUS_LADDER = fable.Ladder(
    key="opus", label="Opus", candidates=OPUS_CANDIDATES, model_env=OPUS_MODEL_ENV
)


def opus_model(transport: str | None = None) -> str:
    """The Opus model id this process will actually ask for (the `opus` ladder)."""
    return fable.ladder_model(OPUS_LADDER, transport)


def opus_spec(transport: str | None = None) -> fable.ClaudeSpec:
    """The `opus` spec with its model resolved through the ladder."""
    return fable.ladder_spec(OPUS_LADDER, transport)


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
    """Run one Opus turn — same contract as ``fable.run`` (never raises for
    expected failures; ``resume`` continues a prior SDK session; ``on_think``
    streams reasoning blocks live on the SDK path). Laddered newest-first, so a
    too-old Claude Code build degrades to Opus 5 rather than failing."""
    return await fable.run(
        question,
        context,
        resume=resume,
        timeout=timeout,
        use_cli=use_cli,
        system_prompt=system_prompt,
        on_think=on_think,
        ladder=OPUS_LADDER,
    )
