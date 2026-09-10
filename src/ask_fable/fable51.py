"""Invoke Fable 5.1 (claude-fable-5-1) explicitly, without the ladder.

`fable` resolves its model at call time (``fable.fable_model()``), which is what
you want almost always — it tracks the newest Fable on its own. This module is
the escape hatch for when you need to NAME the model instead: the `fable51`
oracle token always means claude-fable-5-1 and keeps meaning it after the ladder
has moved on to a later release.

There is no separate transport and no `ask_fable51` tool: this is a thin wrapper
over ``fable.run`` with a pinned :class:`~ask_fable.fable.ClaudeSpec`, exactly as
``opus.py`` is, so it rides the same Claude Agent SDK path (Claude Code's OAuth
session, tools disabled) with the `claude` CLI as the same fallback.

Note that a pinned call does NOT fall back: if the local Claude Code build is too
old for 5.1 the turn fails with ``model_unavailable`` and says so, rather than
quietly answering as Fable 5 under the name you pinned.
"""

from __future__ import annotations

from collections.abc import Callable

from . import fable
from .oracle_common import OracleResult

FABLE51_MODEL = fable.FABLE_PREFERRED_MODEL


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
    """Run one Fable 5.1 turn — same contract as ``fable.run``, model pinned."""
    return await fable.run(
        question,
        context,
        resume=resume,
        timeout=timeout,
        use_cli=use_cli,
        system_prompt=system_prompt,
        on_think=on_think,
        spec=fable.FABLE51,
    )
