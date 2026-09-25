"""Claude Haiku 4.5 as an internal UTILITY worker — deliberately NOT an oracle.

Haiku is fast, cheap, and free on the same OAuth session as `ask`/`ask_opus5`, which
makes it the right tool for the narrow, mechanical NLP subtasks inside the pipeline
(paraphrasing, extraction, classification) that don't need a reasoning oracle. It is
kept OUT of the oracle registry on purpose:

- **Never a council seat.** It shares Fable's and Opus 5's training lineage, so it adds
  no council diversity — the very reason it isn't in ``oracles.KNOWN`` /
  ``_ANTHROPIC_BRIDGES``. It is not reachable as a `models=[...]` token, and no tier or
  schema enumerates it.
- **Never an enforcement floor.** A worker may *flag* something for a human, never
  *decide* a gate — the guard and redaction floors live below the model.

This module is the shared home for those internal uses; the first is the metamorphic
paraphraser in ``falsify.py``. It rides ``fable.run`` with a pinned Haiku
:class:`~ask_fable.fable.ClaudeSpec` (same Claude Agent SDK / CLI path as every other
OAuth model), so telemetry still attributes a `haiku` provider span honestly even
though the model is never offered as an oracle. Like every pinned spec it does NOT
fall back: too old a Claude Code build returns ``model_unavailable`` and the caller
decides what to do (the paraphraser, for instance, falls back to a cross-lab model).
"""

from __future__ import annotations

from . import fable
from .oracle_common import OracleResult

HAIKU_MODEL = "claude-haiku-4-5"
HAIKU = fable.ClaudeSpec(model=HAIKU_MODEL, key="haiku", label="Haiku 4.5")


async def run(prompt: str, context: str = "", *, timeout: float | None = None) -> OracleResult:
    """One Haiku turn for a bounded utility task — same contract as ``fable.run``
    (never raises for expected failures), model pinned. No `on_think`/`resume`: a
    worker call is single-shot and its reasoning isn't surfaced."""
    return await fable.run(prompt, context, timeout=timeout, spec=HAIKU)
