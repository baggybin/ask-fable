"""The debate-ledger contract — a SECOND machine-readable block, alongside the
sidecar, that carries the structured state of an ``ask_debate`` turn.

The adversarial debate mode (``ask_debate``) threads a question through PROPOSE →
REFUTE → REVISE → (REBUT) → ADJUDICATE. Each turn stays free prose (with its normal
``json-sidecar`` decision block, untouched here) and appends ONE extra fenced block
whose info-string is ``json-debate`` carrying the turn's role-specific ledger:

- ``propose``     → ``claims``       (the load-bearing claims the answer rests on)
- ``refute``      → ``dispositions`` (concede/contest per claim) + ``added_claims``
- ``revise``      → ``resolutions``  (defended/revised/withdrawn per contested claim)
- ``adjudicate``  → ``rulings`` + ``decisive_argument``

WHY a separate block rather than growing the sidecar: the sidecar is deliberately a
slim 3-field contract because ``sidecar.extract`` treats it as the turn's decision
signal and FAILS SAFE to ``None`` on any malformation — folding nested, role-varying
arrays into it would make a mid-tier model's mangled JSON cost the whole turn its
recommendation/confidence. A separate fence decouples the two: a bad ledger degrades
only the ledger (the debate falls through to adjudication), never the sidecar. The
sidecar parser already ignores foreign fences, so existing tools are unaffected.

This module mirrors ``sidecar``'s discipline exactly: same loose-JSON repair, same
fail-safe (trust the value only when EXACTLY ONE ``json-debate`` block is present,
else ``None``), and it always STRIPS every identified block from the prose so a raw
ledger never leaks into the answer. Call it AFTER ``sidecar.extract`` on the prose
that already has the sidecar removed.

The resolution predicates at the bottom are the DETERMINISTIC, server-side control
flow for the debate — the models never self-report "we're done"; the handler decides
{conceded | converged | adjudicated | stalemate} from the parsed ledgers alone.
"""

from __future__ import annotations

import json
import re

FENCE = "json-debate"  # distinctive info-string — never collides with a `json-sidecar` block
SENTINEL = "debate_version"  # required key; its presence disambiguates the block
ROLES = ("propose", "refute", "revise", "adjudicate")
SEVERITIES = ("low", "medium", "high")

_FENCE_RE = re.compile(r"```([A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)


def _loose_json(blob: str) -> dict | None:
    """Strict parse, then ONE bounded repair pass (smart/single quotes, trailing
    commas) — identical policy to ``sidecar``. Anything worse is treated as absent."""
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


def extract(text: str) -> tuple[str, dict | None]:
    """Return ``(prose_without_ledger, ledger_or_None)``.

    Same two-tier + fail-safe rules as ``sidecar.extract``: prefer explicit
    ``json-debate`` fences, else a plain block carrying the ``debate_version``
    sentinel; strip EVERY identified block from the prose (no leaks), but trust the
    VALUE only when exactly one block is identified. Call this on prose that has
    already had the sidecar stripped."""
    text = text or ""
    parsed = [(m, _loose_json(m.group(2))) for m in _FENCE_RE.finditer(text)]
    fenced = [(m, obj) for (m, obj) in parsed if m.group(1).strip().lower() == FENCE]
    candidates = fenced or [
        (m, obj) for (m, obj) in parsed if isinstance(obj, dict) and SENTINEL in obj
    ]
    if not candidates:
        return text.strip(), None
    prose_parts: list[str] = []
    cur = 0
    for m, _ in sorted(candidates, key=lambda c: c[0].start()):
        prose_parts.append(text[cur:m.start()])
        cur = m.end()
    prose_parts.append(text[cur:])
    prose = "".join(prose_parts).strip()
    if len(candidates) == 1 and isinstance(candidates[0][1], dict):
        return prose, candidates[0][1]
    return prose, None


# --- Field accessors (defensive — a model's ledger may be partial/malformed) -----

def _rows(ledger: dict | None, field: str) -> list[dict]:
    """The list under ``field``, keeping only dict rows; [] for anything else."""
    if not isinstance(ledger, dict):
        return []
    rows = ledger.get(field)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def claims(ledger: dict | None) -> list[dict]:
    return _rows(ledger, "claims")


def dispositions(ledger: dict | None) -> list[dict]:
    return _rows(ledger, "dispositions")


def added_claims(ledger: dict | None) -> list[dict]:
    return _rows(ledger, "added_claims")


def resolutions(ledger: dict | None) -> list[dict]:
    return _rows(ledger, "resolutions")


# --- Rendering (ledger → readable text injected into the next turn's prompt) -----

def render_claims(ledger: dict | None) -> str:
    """The proposer's claims as a numbered list for the opponent to dispose of."""
    out = []
    for c in claims(ledger):
        cid = str(c.get("id") or "?")
        lb = " (load-bearing)" if c.get("load_bearing") else ""
        out.append(f"- [{cid}]{lb} {str(c.get('claim') or '').strip()}"
                    + (f"\n  evidence: {str(c['evidence']).strip()}" if c.get("evidence") else ""))
    return "\n".join(out) or "(the proposer emitted no parseable claims)"


def render_dispositions(ledger: dict | None) -> str:
    """The opponent's per-claim dispositions for the proposer to answer in REVISE."""
    out = []
    for d in dispositions(ledger):
        cid = str(d.get("id") or "?")
        verdict = str(d.get("verdict") or "?")
        line = f"- [{cid}] {verdict}: {str(d.get('reason') or '').strip()}"
        if d.get("verdict") == "contest":
            if d.get("failure_scenario"):
                line += f"\n  failure: {str(d['failure_scenario']).strip()}"
            if d.get("severity"):
                line += f" (severity: {d['severity']})"
        out.append(line)
    for a in added_claims(ledger):
        out.append(f"- [{str(a.get('id') or '?')}] (missing claim added by opponent): "
                   f"{str(a.get('claim') or '').strip()}")
    return "\n".join(out) or "(the opponent emitted no parseable dispositions)"


def render_open_claims(propose_l: dict | None, open_ids: list[str],
                       refute_l: dict | None = None) -> str:
    """Just the still-open claims, for a round-2 rebuttal prompt. ``open_ids`` can
    include opponent-ADDED claims (B1, …), so their text is looked up in the refute
    ledger, not just the proposer's."""
    by_id = {str(c.get("id")): c for c in claims(propose_l)}
    by_id.update({str(a.get("id")): a for a in added_claims(refute_l)})
    out = []
    for cid in open_ids:
        c = by_id.get(cid)
        text = str(c.get("claim")).strip() if c else "(claim text unavailable)"
        out.append(f"- [{cid}] {text}")
    return "\n".join(out) or "(no open claims)"


def render_for_adjudicator(propose_l: dict | None, refute_l: dict | None,
                           revise_l: dict | None, open_ids: list[str],
                           rebut_l: dict | None = None) -> str:
    """The full anonymized ledger for the adjudicator — only the still-contested
    claims matter, but the full chain is shown so a ruling can cite the evidence.
    ``rebut_l`` (round 2) is included when present: those are the opponent's freshest
    arguments and must not be invisible to the judge."""
    parts = ["POSITION 1 CLAIMS:", render_claims(propose_l),
             "\nPOSITION 2 DISPOSITIONS:", render_dispositions(refute_l)]
    if revise_l is not None:
        res = "\n".join(
            f"- [{str(r.get('id') or '?')}] {str(r.get('status') or '?')}: {str(r.get('note') or '').strip()}"
            for r in resolutions(revise_l)
        ) or "(no parseable resolutions)"
        parts += ["\nPOSITION 1 REVISIONS:", res]
    if rebut_l is not None:
        parts += ["\nPOSITION 2 REBUTTAL (round 2):", render_dispositions(rebut_l)]
    if open_ids:
        parts.append("\nSTILL CONTESTED (rule on these): " + ", ".join(open_ids))
    return "\n".join(parts)


# --- Deterministic resolution predicates (server-side control flow) --------------

def contested_after_refute(refute_l: dict | None) -> list[str]:
    """Claim ids the opponent contested, plus any it ADDED (added == contested by
    definition). Order-preserving, de-duped."""
    ids: list[str] = []
    for d in dispositions(refute_l):
        if d.get("verdict") == "contest" and d.get("id"):
            ids.append(str(d["id"]))
    for a in added_claims(refute_l):
        if a.get("id"):
            ids.append(str(a["id"]))
    seen: set[str] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


def severity_map(refute_l: dict | None) -> dict[str, str]:
    """id → severity from the opponent's contests; added claims default to medium."""
    sev = {str(a["id"]): "medium" for a in added_claims(refute_l) if a.get("id")}
    for d in dispositions(refute_l):
        if d.get("verdict") == "contest" and d.get("id"):
            s = d.get("severity")
            sev[str(d["id"])] = s if s in SEVERITIES else "medium"
    return sev


def open_after_revise(contested: list[str], revise_l: dict | None) -> list[str]:
    """Contested ids the proposer did NOT close. Unaddressed contests stay OPEN —
    defence-by-silence does not win a claim. ``revised``/``withdrawn`` always close;
    ``defended`` closes only with a non-empty note (the new argument the prompt
    demands) — a bare assertion must not let the proposer escape adjudication."""
    rows = {str(r.get("id")): r for r in resolutions(revise_l) if r.get("id")}

    def _closed(cid: str) -> bool:
        r = rows.get(cid)
        if not isinstance(r, dict):
            return False
        status = r.get("status")
        if status in ("revised", "withdrawn"):
            return True
        return status == "defended" and bool(str(r.get("note") or "").strip())

    return [cid for cid in contested if not _closed(cid)]


def low_effort_opposition(refute_l: dict | None) -> bool:
    """True when the opponent conceded claim(s) without recording any attempted
    refutation — a rubber-stamp concession, the sycophancy failure mode."""
    concedes = [d for d in dispositions(refute_l) if d.get("verdict") == "concede"]
    return bool(concedes) and all(not str(d.get("attempted_refutation") or "").strip() for d in concedes)


def still_contested_after_rebut(rebut_l: dict | None) -> list[dict]:
    """The opponent's still-contesting dispositions in a round-2 rebuttal."""
    return [d for d in dispositions(rebut_l) if d.get("verdict") == "contest"]


def is_stalemate(rebut_l: dict | None, open_ids: list[str]) -> bool:
    """Round-2 stalemate: every remaining contest is tagged ``restated`` and covers
    no ground beyond the already-open claims — models grinding identical positions."""
    still = still_contested_after_rebut(rebut_l)
    if not still:
        return False
    all_restated = all(d.get("novelty") == "restated" for d in still)
    within = {str(d.get("id")) for d in still} <= set(open_ids)
    return all_restated and within


_CONF_DOWNGRADE = {"high": "medium", "medium": "low", "low": "low"}


def downgrade_confidence(conf: str | None) -> str | None:
    """One notch down — applied mechanically on a stalemate, not at model discretion."""
    return _CONF_DOWNGRADE.get(conf, conf)
