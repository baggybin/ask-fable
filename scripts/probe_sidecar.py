#!/usr/bin/env python3
"""Capability probe for the PR1 structured-sidecar contract.

The council's central instruction was: DESIGN THE SIDECAR SCHEMA/PARSER AGAINST
WHAT THE BACKENDS ACTUALLY EMIT, not against an inferred failure model. This
script sends every reachable backend one realistic engineering question plus the
candidate sidecar instruction (in the USER turn, because only ``fable.run`` takes
a ``system_prompt`` override — the rest hardcode ``MINIMAX_SYSTEM_PROMPT``), then
runs the *candidate* extract-and-strip parser over each raw reply and reports how
well each backend complied.

Run it BEFORE freezing the schema:

    .venv/bin/python scripts/probe_sidecar.py                 # all reachable backends
    .venv/bin/python scripts/probe_sidecar.py fable glm       # a subset
    ASK_FABLE_PROBE_OLLAMA=kimi-k2.7-code:cloud .venv/bin/python scripts/probe_sidecar.py

Raw replies are written to the scratchpad/OUT dir so you can eyeball what each
model really did (fence style, JSON validity, prose-in-fence, truncation).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from ask_fable import oracles

# --- The candidate contract -------------------------------------------------
# A distinctive fence info-string + sentinel key so the parser never grabs an
# incidental ```json block the answer used as *content*. Slim required core
# (recommendation / confidence / needs_context) per the council verdict.
SIDECAR_FENCE = "json-sidecar"
SENTINEL_KEY = "sidecar_version"
REC_VALUES = {"apply", "investigate", "reject", "needs_more_context"}
CONF_VALUES = {"low", "medium", "high"}

SIDECAR_INSTRUCTION = f"""

---
FORMAT REQUIREMENT: After your prose answer, append EXACTLY ONE fenced code block
whose info-string is `{SIDECAR_FENCE}` (a line of three backticks immediately
followed by `{SIDECAR_FENCE}`), containing a single JSON object and nothing else:

```{SIDECAR_FENCE}
{{
  "{SENTINEL_KEY}": 1,
  "recommendation": "apply" | "investigate" | "reject" | "needs_more_context",
  "confidence": "low" | "medium" | "high",
  "needs_context": ["<=3 short strings naming code/files you'd need pasted; [] if none"]
}}
```

Rules: put NO prose inside the block; emit it exactly once, as the very last thing
in your response; the JSON must parse. The prose above the block remains the real
answer — the block only summarizes it.""".strip()

# A realistic engineering question that naturally yields a recommendation +
# confidence, so we exercise the *whole* contract, not just "can it emit JSON".
PROBE_QUESTION = (
    "In the function below, should I cache `_expensive(key)` with "
    "`functools.lru_cache` or a hand-rolled dict keyed on `key`? Pick ONE and "
    "say why in a couple of sentences."
)
PROBE_CONTEXT = (
    "def handle(key: str) -> Result:\n"
    "    # called ~thousands of times per request; `key` is a short str;\n"
    "    # `_expensive` is pure and returns a small immutable Result.\n"
    "    return _expensive(key)\n"
)


# --- Candidate extract-and-strip parser (reused into the real feature later) --
_FENCE_RE = re.compile(r"```([A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)


def _loose_json(blob: str) -> dict | None:
    """Strict parse, then a single bounded repair pass (trailing commas / smart
    quotes / single quotes). Anything worse than that is treated as absent."""
    for candidate in (blob, _repair(blob)):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _repair(blob: str) -> str:
    b = blob.strip()
    b = b.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    b = re.sub(r",(\s*[}\]])", r"\1", b)  # trailing commas
    return b


def extract_sidecar(text: str) -> tuple[str, dict | None, str]:
    """Return (prose_without_sidecar, sidecar_or_None, note).

    Contract: find the LAST fence whose info-string is `json-sidecar` OR whose
    body parses to an object carrying the sentinel key; strip it from the prose;
    require the sentinel + a recognized `recommendation`. On any failure, return
    the full text as prose with sidecar=None (missing_sidecar path)."""
    matches = list(_FENCE_RE.finditer(text or ""))
    chosen = None
    for m in matches:
        info, body = m.group(1).strip().lower(), m.group(2)
        obj = _loose_json(body)
        looks_like = info == SIDECAR_FENCE or (isinstance(obj, dict) and SENTINEL_KEY in obj)
        if looks_like and obj is not None:
            chosen = (m, obj)  # keep scanning; last one wins
    if chosen is None:
        return (text or "").strip(), None, "missing_sidecar"
    m, obj = chosen
    if SENTINEL_KEY not in obj or obj.get("recommendation") not in REC_VALUES:
        return (text or "").strip(), None, "sidecar_unrecognized"
    prose = (text[: m.start()] + text[m.end():]).strip()
    trailing = text[m.end():].strip()
    note = "ok" if not trailing else "ok_prose_after_sidecar"
    return prose, obj, note


def _analyze(model: str, status: str, secs: float, text: str) -> dict:
    prose, sidecar, note = extract_sidecar(text)
    fenced = list(_FENCE_RE.finditer(text or ""))
    fence_styles = [m.group(1).strip().lower() or "(none)" for m in fenced]
    rec = (sidecar or {}).get("recommendation")
    conf = (sidecar or {}).get("confidence")
    nc = (sidecar or {}).get("needs_context")
    compliant = (
        status == "ok"
        and sidecar is not None
        and SIDECAR_FENCE in fence_styles
        and rec in REC_VALUES
        and conf in CONF_VALUES
        and isinstance(nc, list)
        and note == "ok"
    )
    return {
        "model": model,
        "status": status,
        "latency_s": round(secs, 1),
        "raw_len": len(text or ""),
        "fence_styles": fence_styles,
        "used_sidecar_fence": SIDECAR_FENCE in fence_styles,
        "sidecar_parsed": sidecar is not None,
        "sentinel_ok": bool(sidecar and SENTINEL_KEY in sidecar),
        "recommendation": rec,
        "recommendation_valid": rec in REC_VALUES,
        "confidence": conf,
        "confidence_valid": conf in CONF_VALUES,
        "needs_context_is_list": isinstance(nc, list),
        "parser_note": note,
        "prose_len_after_strip": len(prose),
        "verdict": "COMPLIANT" if compliant else "NON-COMPLIANT",
    }


def _out_dir() -> Path:
    base = os.environ.get("ASK_FABLE_PROBE_OUT") or "."
    d = Path(base) / "sidecar_probe"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _probe_one(key: str) -> dict:
    q = f"{PROBE_QUESTION}\n{SIDECAR_INSTRUCTION}"
    t0 = time.monotonic()
    res = await oracles.run(key, q, PROBE_CONTEXT)
    secs = time.monotonic() - t0
    text = res.text if res.status == "ok" else f"[{res.status}:{res.kind}] {res.text}"
    report = _analyze(res.model or key, res.status, secs, text)
    report["key"] = key
    (_out_dir() / f"{key.replace(':', '_')}.md").write_text(
        f"# {key} -> {res.model}\n\nstatus={res.status} latency={secs:.1f}s\n\n"
        f"## verdict\n{report['verdict']} (note={report['parser_note']})\n\n## raw\n\n{text}\n",
        encoding="utf-8",
    )
    return report


async def main(keys: list[str]) -> int:
    extra = os.environ.get("ASK_FABLE_PROBE_OLLAMA")
    if extra:
        keys.append(f"{oracles.OLLAMA_PREFIX}{extra}")
    reachable = [k for k in keys if oracles.available(k)]
    skipped = [k for k in keys if not oracles.available(k)]
    if skipped:
        print(f"skipping unreachable backends: {', '.join(skipped)}", file=sys.stderr)
    if not reachable:
        print("no reachable backends — nothing to probe", file=sys.stderr)
        return 1
    print(f"probing (parallel): {', '.join(reachable)}  — 1-3 min each", file=sys.stderr)
    reports = await asyncio.gather(*(_probe_one(k) for k in reachable))
    print(json.dumps(reports, indent=2))
    ncompliant = sum(r["verdict"] == "COMPLIANT" for r in reports)
    print(
        f"\n{ncompliant}/{len(reports)} compliant · raw replies in {_out_dir()}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    args = [a.lower() for a in sys.argv[1:]] or list(oracles.KNOWN)
    raise SystemExit(asyncio.run(main(args)))
