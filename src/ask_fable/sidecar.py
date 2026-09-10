"""The structured-sidecar contract (PR1a).

Every answer stays free prose, but the model is asked to append ONE fenced block
with a distinctive info-string (``json-sidecar``) carrying a slim, machine-actionable
summary. The server EXTRACTS-AND-STRIPS that block from ``answer`` (so harnesses
still render clean prose) and surfaces it as an additive ``sidecar`` field.

Design was validated by ``scripts/probe_sidecar.py`` against every reachable
backend (Fable, MiniMax, Gemini, GLM, DeepSeek) — all emitted a valid block on the
first try, and the distinctive fence + sentinel key correctly ignored incidental
``​```python``/``​```json`` blocks the prose used as content.

Kept dependency-free and self-contained: no salient-core import (its IDEAS informed
this — untrusted-content discipline, a deliverable-shape contract — but ask_fable
owns its own copy). The slim required core is deliberate: weaker backends choke on
a six-field schema while also writing prose.
"""

from __future__ import annotations

import json
import re

FENCE = "json-sidecar"  # distinctive info-string so we never grab a content ```json block
SENTINEL = "sidecar_version"  # required key; its presence disambiguates the block
REC_VALUES = ("apply", "investigate", "reject", "needs_more_context")
CONF_VALUES = ("low", "medium", "high")

# Appended to the shared oracle system prompt. Delivered via the system channel for
# every backend (each references the same prompt constant); the probe validated the
# equivalent user-turn delivery on all five backends.
INSTRUCTION = f"""\
After your prose answer, append EXACTLY ONE fenced code block whose info-string is \
`{FENCE}` — a line of three backticks immediately followed by `{FENCE}` — containing \
a single JSON object and nothing else:

```{FENCE}
{{"{SENTINEL}": 1, "recommendation": "apply|investigate|reject|needs_more_context", \
"confidence": "low|medium|high", "needs_context": ["<=3 short strings naming code/files \
you'd need pasted to be more certain; [] if none"]}}
```

The prose above the block is the real answer; the block only summarizes it — never put \
a recommendation in the block that the prose doesn't support, and put NO prose inside \
the block. Emit it exactly once, as the very last thing in your response. `recommendation` \
is `needs_more_context` when the pasted context was too thin to answer well. If you REFUSE \
(reply `REFUSED: <reason>`), output only that one line and no block."""

_FENCE_RE = re.compile(r"```([A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)


def _loose_json(blob: str) -> dict | None:
    """Strict parse, then ONE bounded repair pass (smart/single quotes, trailing
    commas). Anything needing more than that is treated as absent — we never fight
    a malformed block, we fall back to prose."""
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


def _normalize(obj: dict) -> dict:
    """Coerce a parsed block to the known shape; drop unknown/invalid values so a
    caller can trust the field types without re-validating."""
    rec = obj.get("recommendation")
    conf = obj.get("confidence")
    nc = obj.get("needs_context")
    return {
        "recommendation": rec if rec in REC_VALUES else None,
        "confidence": conf if conf in CONF_VALUES else None,
        "needs_context": [str(x) for x in nc] if isinstance(nc, list) else [],
    }


def extract(text: str) -> tuple[str, dict | None]:
    """Return (prose_without_sidecar, sidecar_or_None).

    Identify sidecar blocks in two tiers: first any fence whose info-string is
    ``json-sidecar``; only if there are none, fall back to a plain block whose body
    parses to an object carrying the sentinel key. **Every** identified block is
    stripped from the prose — so a raw block never leaks into ``answer`` even when its
    payload fails to validate. The sidecar VALUE is trusted only when exactly ONE
    block is identified: a model that emits several sidecar-shaped blocks is
    non-compliant, so we fail safe to ``None`` rather than surface a possibly-decoy
    recommendation. On no identifiable block the full text is returned with
    ``sidecar=None`` (the caller flags ``missing_sidecar``)."""
    text = text or ""
    parsed = [(m, _loose_json(m.group(2))) for m in _FENCE_RE.finditer(text)]
    # Tier 1: explicit json-sidecar fences (kept even if the body didn't parse, so a
    # malformed-but-clearly-the-sidecar block is still stripped). Tier 2 fallback:
    # plain blocks carrying the sentinel key — only when no explicit fence was used.
    fenced = [(m, obj) for (m, obj) in parsed if m.group(1).strip().lower() == FENCE]
    candidates = fenced or [
        (m, obj) for (m, obj) in parsed if isinstance(obj, dict) and SENTINEL in obj
    ]
    if not candidates:
        return text.strip(), None
    # Strip every identified block regardless of payload validity — no leaks.
    prose_parts: list[str] = []
    cur = 0
    for m, _ in sorted(candidates, key=lambda c: c[0].start()):
        prose_parts.append(text[cur:m.start()])
        cur = m.end()
    prose_parts.append(text[cur:])
    prose = "".join(prose_parts).strip()
    # Trust the value only when the model emitted exactly one sidecar block.
    if len(candidates) == 1 and isinstance(candidates[0][1], dict):
        return prose, _normalize(candidates[0][1])
    return prose, None


def decorate(payload: dict, answer: str) -> dict:
    """Split ``answer`` into clean prose + sidecar and fold both into an ``ok``
    payload. Mutates and returns ``payload``. Additive: on a miss it sets
    ``sidecar: None`` + ``missing_sidecar: True`` and leaves the prose intact, so no
    existing harness breaks."""
    prose, sidecar = extract(answer)
    payload["answer"] = prose
    payload["sidecar"] = sidecar
    payload["missing_sidecar"] = sidecar is None
    return payload
