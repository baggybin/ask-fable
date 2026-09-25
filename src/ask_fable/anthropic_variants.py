"""Extra pinned Anthropic models on the same OAuth session as `ask` / `ask_opus5`.

`opus.py` and `fable51.py` each pin ONE model with a bespoke module. That pattern
does not survive a fourth or fifth pin — the modules are byte-for-byte identical
apart from a :class:`~ask_fable.fable.ClaudeSpec`. So the additional variants live
here as a TABLE of specs, each wrapped in a module-shaped bridge so
``oracles._ANTHROPIC_BRIDGES`` can treat it exactly like ``opus``: a value with a
``.run`` coroutine and a ``.MODEL`` attribute.

What's here and why (see docs/decisions or the council that settled it):

- ``sonnet`` (claude-sonnet-5) — a cheap, fast Anthropic voice for high-volume or
  lower-stakes turns, exposed as the single-turn ``ask_sonnet`` tool and usable as
  a chain/council token. NOT a council-tier member.
- ``opus55`` / ``opus5`` (claude-opus-5-5 / claude-opus-5) — specific Opus
  versions, pinned. ``opus`` itself is a LADDER (``opus.OPUS_LADDER``) that tracks
  the newest, so these are how a caller NAMES one version and keeps meaning it once
  the ladder has moved on — exactly as ``fable51`` pins a Fable id.
- ``opus48`` (claude-opus-4-8) — the previous Opus generation, pinned for
  regression / A-B work against the current ``opus``. Token only — no dedicated tool.

These are SAME-LAB variants: they add no council diversity (their errors correlate
with Fable's and Opus's), so they are excluded from every council tier
(``oracles._TIER_EXCLUDED``) and only ever earn a seat when a caller NAMES them.
Like every pinned spec, a pinned call does NOT fall back: too-old a Claude Code
build fails with ``model_unavailable`` rather than quietly answering as a different
model under the name you pinned.
"""

from __future__ import annotations

from collections.abc import Callable

from . import fable, opus
from .oracle_common import OracleResult

# Pinned specs, keyed by oracle-registry key. Add a row to register a variant;
# `oracles.py` reads this table for KNOWN membership, dispatch, and labels. The
# Opus version ids are sourced from `opus.py` so the ladder and its pins can never
# drift apart.
SPECS: dict[str, fable.ClaudeSpec] = {
    "opus55": fable.ClaudeSpec(model=opus.OPUS_PREFERRED_MODEL, key="opus55", label="Opus 5.5"),
    "opus5": fable.ClaudeSpec(model=opus.OPUS_MODEL, key="opus5", label="Opus 5"),
    "opus48": fable.ClaudeSpec(model="claude-opus-4-8", key="opus48", label="Opus 4.8"),
    "sonnet": fable.ClaudeSpec(model="claude-sonnet-5", key="sonnet", label="Sonnet 5"),
}


class _Variant:
    """A module-shaped bridge over one pinned spec.

    ``oracles._ANTHROPIC_BRIDGES`` dispatches with ``bridge.run(question, context,
    system_prompt=..., on_think=...)`` and ``label()`` reads a module's model
    constant; this class exposes both surfaces (``.run`` and ``.MODEL``) so a spec
    in :data:`SPECS` drops in beside the hand-written ``opus`` / ``fable51``
    modules with no special-casing in the registry."""

    def __init__(self, spec: fable.ClaudeSpec) -> None:
        self.spec = spec
        self.MODEL = spec.model

    async def run(
        self,
        question: str,
        context: str = "",
        *,
        resume: str | None = None,
        timeout: float | None = None,
        use_cli: bool | None = None,
        system_prompt: str | None = None,
        on_think: Callable[[str], None] | None = None,
    ) -> OracleResult:
        """One turn on this pinned model — same contract as ``fable.run``, spec
        pinned (no ladder, no fallback)."""
        return await fable.run(
            question,
            context,
            resume=resume,
            timeout=timeout,
            use_cli=use_cli,
            system_prompt=system_prompt,
            on_think=on_think,
            spec=self.spec,
        )


# key -> module-shaped bridge, merged into oracles._ANTHROPIC_BRIDGES.
BRIDGES: dict[str, _Variant] = {key: _Variant(spec) for key, spec in SPECS.items()}
