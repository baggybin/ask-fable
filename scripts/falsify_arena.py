#!/usr/bin/env python3
"""Run the ``ask_falsify`` acceptance harness over a claim set.

Usage::

    python -m scripts.falsify_arena <claimset.json> [--rounds N] [--out report.json]

``claimset.json`` is a JSON list of ``ask_falsify`` arg dicts — each needs at least
``question`` + ``session``, and usually ``context`` (the corpus the claims must cite).
This makes REAL model calls. It prints a per-item verdict plus the acceptance metrics
(``grep_ratio``, the executor-off ``prevented`` count); ``--out`` writes the full report.

Read the verdict as: ``retrieval-bound`` is EXPECTED for v1 (cite/contra only) — the bar
is cleared (``frontier-probing``) once a ``run:``/``metamorph:`` receipt manufactures a
kill against a claim whose answer was not in the pack.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ask_fable import falsify


def _fmt(r: dict) -> str:
    if r.get("status") != "ok":
        return f"  [{r.get('session')}] ERROR: {r.get('status')} — {r.get('detail')}"
    gr = r.get("grep_ratio")
    return (f"  [{r.get('session')}] {r['verdict']}  "
            f"killed={r['killed']} survived={r['survived']} "
            f"grep_ratio={'n/a' if gr is None else f'{gr:.2f}'} "
            f"prevented={r['ablation']['prevented']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ask_falsify acceptance harness")
    ap.add_argument("claimset", help="JSON file: a list of ask_falsify arg dicts")
    ap.add_argument("--rounds", type=int, default=None, help="assert→attack cycles per item")
    ap.add_argument("--out", type=Path, default=None, help="write the full JSON report here")
    args = ap.parse_args(argv)

    items = json.loads(Path(args.claimset).read_text())
    if not isinstance(items, list):
        print("claimset must be a JSON list of ask_falsify arg dicts", file=sys.stderr)
        return 2

    results = asyncio.run(falsify.run_arena(items, rounds=args.rounds))
    print("ask_falsify arena:")
    for r in results:
        print(_fmt(r))
    n_ok = sum(1 for r in results if r.get("status") == "ok")
    n_probe = sum(1 for r in results if r.get("verdict") == "frontier-probing")
    print(f"\n{n_ok}/{len(results)} ran; {n_probe} cleared the frontier bar "
          "(retrieval-bound is expected for v1).")
    if args.out:
        args.out.write_text(json.dumps(results, indent=2))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
