"""CLI entry: ``python3 -m ask_fable`` (also the ``ask-fable`` console script).

stdio is the default transport (one server per harness/agent). ``--transport
http`` is reserved for a shared multi-agent process and is not implemented yet.
"""

from __future__ import annotations

import argparse

from . import serve


def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ask-fable",
        description="Gated MCP server routing narrow code questions to Fable.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport (default: stdio). http is reserved for a shared process (not yet implemented).",
    )
    args = parser.parse_args(argv)

    if args.transport == "http":
        raise SystemExit("http transport is not implemented yet; use --transport stdio")
    serve()


if __name__ == "__main__":
    _main()
