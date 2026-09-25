#!/usr/bin/env python3
"""Run the ``ask_verify`` acceptance harness over a SEEDED draft set.

Usage::

    python -m scripts.verify_arena <draftset.json> [--out report.json]

``draftset.json`` is a JSON list of ``ask_verify`` arg dicts, each with two extra keys:

    {"label": "cap-off-by-25",
     "seeded": true,                  # this draft carries a KNOWN injected fault
     "question": "Is the retry capped?",
     "answer":   "The retry caps at 30 seconds.",
     "context":  "CAP = 5  # seconds"}

``seeded: false`` marks a draft we believe is CORRECT — the reviewer must leave it alone.

Why this exists. The in-tool metrics (`verify.prevented` / `unbacked_objections`) measure
whether the reviewer had an EFFECT and whether its objections were backed. They cannot
measure whether the objections were RIGHT: a confident wrong objection with a real quote
attached counts exactly like a true one. The pre-registered alternative — have a held-out
model judge whether revised answers beat originals — was dropped, because a model grading
a model certifies agreement rather than correctness and is gameable by fluency.

Seeding our own faults is the way out. We know the answer because we planted it, so no
model has to adjudicate anything. This is mutation testing pointed at the reviewer.

Read the two numbers as a pair:
  - ``precision_on_seeded``   — of the faults we planted, how many drew a BACKED objection.
    Low means the reviewer is decoration.
  - ``false_objection_rate``  — of the clean drafts, how many drew a backed objection anyway.
    High means enabling suppression would start deleting correct answers.

Both must clear their bars before ``ask_verify`` is allowed to suppress anything. Until
then it only annotates, and that is the entire gate.

Makes REAL model calls — an operator/eval tool, not a unit-tested path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ask_fable import verify


def _fmt(r: dict) -> str:
    label = r.get("label") or "(unlabelled)"
    if r.get("status") != "ok":
        return f"  [{label}] ERROR: {r.get('status')} — {r.get('detail')}"
    kind = "SEEDED" if r.get("seeded") else "clean "
    backed = int(r.get("prevented") or 0)
    # A seeded draft wants a backed objection; a clean one wants none. Mark the miss so a
    # long run is skimmable.
    miss = "  <-- MISS" if (r.get("seeded") and not backed) else (
        "  <-- FALSE OBJECTION" if (not r.get("seeded") and backed) else ""
    )
    sq = r.get("self_quote_ratio")
    return (
        f"  [{label}] {kind} verdict={r.get('verdict'):<14} backed={backed} "
        f"unbacked={r.get('unbacked_objections')} "
        f"self_quote={'n/a' if sq is None else f'{sq:.2f}'}{miss}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ask_verify seeded-fault harness")
    ap.add_argument("draftset", help="JSON file: a list of ask_verify arg dicts + seeded/label")
    ap.add_argument("--out", type=Path, default=None, help="write the full JSON report here")
    args = ap.parse_args(argv)

    items = json.loads(Path(args.draftset).read_text())
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        # Element types checked too: a list of ints passes `isinstance(list)` and then
        # raises AttributeError on `.get`, bypassing this message entirely.
        print("draftset must be a JSON list of ask_verify arg dicts", file=sys.stderr)
        return 2
    if not any(i.get("seeded") for i in items) or not any(not i.get("seeded") for i in items):
        # Precision without a false-objection rate is half a picture: a reviewer that
        # objects to everything scores perfectly on seeded faults alone.
        print(
            "draftset needs BOTH seeded and clean drafts — precision without a "
            "false-objection rate cannot distinguish a good reviewer from a loud one",
            file=sys.stderr,
        )
        return 2

    results = asyncio.run(verify.run_arena(items))
    print("ask_verify arena:")
    for r in results:
        print(_fmt(r))

    report = verify.arena_report(results)
    rec, prec = report["recall_on_seeded"], report["precision"]
    far = report["false_objection_rate"]

    def _pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.2f}"

    skipped = report["runs"] - report["scored"]
    print(
        f"\n{report['scored']}/{report['runs']} scored"
        + (f" ({skipped} not scored — the review itself failed)" if skipped else "")
        + f". caught {report['caught']}/{report['seeded']} seeded faults "
        f"(recall {_pct(rec)}); "
        f"{report['false_objections']}/{report['clean']} false objections on clean drafts "
        f"(rate {_pct(far)}); precision {_pct(prec)}."
    )
    print(
        "Recall alone is not a gate — a reviewer that objects to everything scores 1.00. "
        "Read it beside precision and the false-objection rate."
    )
    print(
        "Suppression stays OFF until precision clears its bar AND the false-objection "
        "rate clears its own — annotation is recoverable, deleting a correct answer is not."
    )
    if args.out:
        args.out.write_text(json.dumps({"runs": results, "report": report}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
