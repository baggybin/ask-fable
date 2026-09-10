"""ask_fable — a gated MCP tool for requesting Fable reasoning.

A small, self-contained MCP server that lets any agent — in Claude Code or
opencode, including ones on other models — request NARROW code/architecture
reasoning from Fable. Fable is reached through Claude Code's existing OAuth
session (via the Claude Agent SDK; the ``claude`` CLI print mode is a fallback);
no API key is set.

A two-layer guard runs before any model call: a non-empty/size sanity floor and a
prohibited-use denylist (the bundled fallback, or salient-core's richer one when
that package is installed). Fable's system prompt is the third layer — it answers
engineering questions (breadth allowed) and refuses cyber/attack content and
non-software domains (biology/medicine refused; neuroscience, cognitive science, AI/ML, and computer science are in-scope).

Run it: ``python3 -m ask_fable`` or the ``ask-fable`` console script.
"""

from __future__ import annotations

from .server import build_server

__all__ = ["build_server", "serve"]


def serve() -> None:
    """Start the server over stdio (the default per-developer transport)."""
    import asyncio
    import os

    # Dedicated MCP server process: every file it ever creates is its own state,
    # so make the umask restrictive at birth. This is what keeps SQLite's
    # WAL/SHM sidecars (created by SQLite, not by us) off the process-umask
    # default of 0o644; the stores additionally chmod as defense-in-depth.
    os.umask(0o077)

    from mcp.server.stdio import stdio_server

    server = build_server()

    async def _main() -> None:
        async with stdio_server() as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    asyncio.run(_main())
