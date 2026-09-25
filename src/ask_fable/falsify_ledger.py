"""The falsify-ledger contract — the persistent, receipt-typed state of an
``ask_falsify`` run, plus the DETERMINISTIC clerk predicates that resolve it.

Where ``ask_debate`` is one-shot and its adjudicator is a MODEL, ``ask_falsify`` is a
stateful process whose resolver is CODE. A claim may *speak* freely, but it cannot
*compound* — move reputation, be rendered as consensus, survive to the next round —
without a receipt this module can verify mechanically. That is the generalization of
salient-core's ``ev:`` receipts + scope-floors to a server with no wire to probe.

This module is PURE: no I/O, no model calls, no ``context_store``. It decides commit /
kill / survive from a ledger dict and the inputs the clerk hands it (a corpus blob for a
``cite`` check, a resolved kill outcome for a resolve pass). The orchestration in
``falsify.py`` does the I/O and the metamorphic re-runs, then calls in here.

Receipt kinds (v1):
- ``cite``      — a verbatim span PRESENT in a loaded corpus (presence, not interpretation).
- ``contra``    — this claim contradicts an already-``survived`` claim (a graph edge).
- ``metamorph`` — the asserting model re-run cold on a perturbed question stayed stable
  (written by the clerk only, tagged ``by: clerk``; a model-supplied one is dropped).
- ``unbacked``  — allowed to appear, ``status: open``, can NEVER compound (default-deny).

It reuses ``debate_ledger``'s parse discipline (loose-JSON repair, fenced-block
extraction with the exactly-one fail-safe, the defensive row accessor) so the two
machine-readable contracts behave identically; only the fence and the semantics differ.
"""

from __future__ import annotations

from copy import deepcopy

from . import debate_ledger

FENCE = "json-falsify"  # never collides with json-debate / json-sidecar
SENTINEL = "falsify_version"  # required key; presence disambiguates the block
RECEIPT_KINDS = ("cite", "contra", "metamorph", "unbacked")
STATUSES = ("open", "killed", "survived")
DEFAULT_K = 2  # failed kill attempts a supported claim must withstand to be `survived`
_MAX_REOPENS = 3  # times a killed claim may be resurrected before it stays dead (bounds cycles)
CLERK_FIELDS = ("ok", "stable", "inconclusive", "by")  # receipt verdicts only the clerk writes


# --- extraction (mirrors debate_ledger.extract, own fence/sentinel) ---------------

def extract(text: str) -> tuple[str, dict | None]:
    """Return ``(prose_without_ledger, ledger_or_None)`` with the same two-tier +
    fail-safe rules as ``debate_ledger.extract``: prefer explicit ``json-falsify``
    fences, else a plain block carrying the ``falsify_version`` sentinel; strip EVERY
    identified block (no leaks) but trust the value only when exactly one is present."""
    text = text or ""
    parsed = [(m, debate_ledger._loose_json(m.group(2))) for m in debate_ledger._FENCE_RE.finditer(text)]
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


# --- accessors (defensive — a persisted ledger may be partial/malformed) ----------

def _rows(ledger: dict | None, field: str) -> list[dict]:
    return debate_ledger._rows(ledger, field)


def claims(ledger: dict | None) -> list[dict]:
    return _rows(ledger, "claims")


def edges(ledger: dict | None) -> list[list]:
    if not isinstance(ledger, dict):
        return []
    rows = ledger.get("edges")
    return [e for e in rows if isinstance(e, (list, tuple)) and len(e) == 3] if isinstance(rows, list) else []


def reputation(ledger: dict | None) -> dict:
    rep = ledger.get("rep") if isinstance(ledger, dict) else None
    return rep if isinstance(rep, dict) else {}


def by_id(ledger: dict | None) -> dict[str, dict]:
    return {str(c.get("id")): c for c in claims(ledger) if c.get("id") is not None}


# --- schema / normalization -------------------------------------------------------

def new_ledger(session: str) -> dict:
    return {"falsify_version": 1, "session": session, "round": 0, "status": "active",
            "claims": [], "edges": [], "rep": {}}


def normalize_claim(raw: dict) -> dict | None:
    """Coerce a raw asserted claim into the canonical shape; ``None`` if it lacks an
    id or claim text (a claim the clerk cannot track)."""
    if not isinstance(raw, dict):
        return None
    cid = str(raw.get("id") or "").strip()
    claim = str(raw.get("claim") or "").strip()
    if not cid or not claim:
        return None
    return {
        "id": cid,
        "author": str(raw.get("author") or "").strip(),
        "domain": str(raw.get("domain") or "general").strip(),
        "claim": claim,
        "status": "open",
        "receipts": [r for r in (raw.get("receipts") or []) if isinstance(r, dict)],
        "kills": [],
        "attempts": 0,
    }


def scrub_receipts(receipts) -> list[dict]:
    """A MODEL's receipts with every clerk-only verdict removed, for the clerk's ingest.
    A ``metamorph`` receipt is dropped outright (only the clerk runs that check) and
    ``CLERK_FIELDS`` are stripped from the rest, so a model can't pre-certify its own
    evidence — ``cite``/``run`` verdicts are then written by the clerk's own checks."""
    if not isinstance(receipts, list):
        return []
    return [
        {k: v for k, v in r.items() if k not in CLERK_FIELDS}
        for r in receipts
        if isinstance(r, dict) and r.get("kind") != "metamorph"
    ]


# --- pure receipt verification (the clerk, given inputs) --------------------------

def cite_supported(receipt: dict, corpus_text: str) -> bool:
    """A ``cite`` receipt holds iff its verbatim ``quote`` is a byte-substring of the
    named corpus. Presence only — "the corpus STATES X" is mechanical; "the corpus
    IMPLIES X" is interpretation and must be marked ``unbacked`` upstream."""
    if not isinstance(receipt, dict) or receipt.get("kind") != "cite":
        return False
    quote = str(receipt.get("quote") or "")
    return bool(quote) and quote in (corpus_text or "")


def has_cycle(existing_edges: list, new_edge: tuple[str, str]) -> bool:
    """True if adding a directed ``src -> dst`` contra edge would create a cycle."""
    src, dst = new_edge
    adj: dict[str, list[str]] = {}
    for e in existing_edges:
        if len(e) == 3 and e[1] == "contra":
            adj.setdefault(str(e[0]), []).append(str(e[2]))
    adj.setdefault(str(src), []).append(str(dst))
    seen: set[str] = set()
    stack = [str(dst)]
    while stack:
        node = stack.pop()
        if node == str(src):
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj.get(node, []))
    return False


def contra_valid(src_id: str, dst_id: str, ledger: dict) -> bool:
    """A ``contra`` is admissible iff both claims exist, the target is ``survived``
    (you contradict something that has standing), and the edge stays acyclic."""
    ids = by_id(ledger)
    if src_id not in ids or dst_id not in ids:
        return False
    if ids[dst_id].get("status") != "survived":
        return False
    return not has_cycle(edges(ledger), (src_id, dst_id))


# --- state transitions (pure — return a new ledger) ------------------------------

def _addresses_kill(raw: dict, cid: str) -> bool:
    """A re-asserted killed claim is only admissible if it ships a receipt whose
    ``addresses`` names the killed id — i.e. it answers the kill, not repeats the claim."""
    for r in (raw.get("receipts") or []):
        if isinstance(r, dict) and str(r.get("addresses") or "") == cid:
            return True
    return False


def no_reassert(ledger: dict, raw: dict) -> bool:
    """True when ``raw`` illegally re-asserts a ``killed`` claim id — either without a
    receipt addressing the kill, or after it has already been resurrected ``_MAX_REOPENS``
    times (a claim can't loop kill→reopen→kill forever). Turns a chat into a process."""
    cid = str(raw.get("id") or "").strip()
    existing = by_id(ledger).get(cid)
    if not existing or existing.get("status") != "killed":
        return False
    if int(existing.get("reopens") or 0) >= _MAX_REOPENS:
        return True  # resurrected too many times — it stays dead
    return not _addresses_kill(raw, cid)


def apply_asserts(ledger: dict, raw_claims: list[dict]) -> tuple[dict, list[str], list[dict]]:
    """Add normalized new claims; drop illegal re-asserts. Returns
    ``(new_ledger, accepted_ids, rejected_raws)``. A re-assert that *addresses* its
    kill reopens the claim to ``open``; restating a live claim only adds receipts —
    its settled status is not the assertor's to reset."""
    led = deepcopy(ledger)
    idx = by_id(led)
    accepted: list[str] = []
    rejected: list[dict] = []
    for raw in raw_claims or []:
        if no_reassert(led, raw):
            rejected.append(raw)
            continue
        norm = normalize_claim(raw)
        if norm is None:
            rejected.append(raw)
            continue
        if norm["id"] in idx:  # existing id — reopen a killed claim, or extend a live one
            existing = idx[norm["id"]]
            existing["receipts"] = existing.get("receipts", []) + norm["receipts"]
            # Only a killed claim reopens. Resetting a SURVIVED one to open let the
            # assertor shield it every round: the falsifier saw it UNSETTLED, a valid
            # contra (which needs a survived target) was refused as a failed attempt,
            # and resolve() then restored `survived`.
            if existing.get("status") == "killed":
                # A legal re-assert that addressed the kill (no_reassert already rejected
                # non-addressing ones). Mark the accepted kills addressed so resolve()
                # doesn't re-kill it this round — a NEW unaddressed accepted kill later
                # still kills, and history is kept. Reset `attempts` so a reopened claim
                # must earn `survived` with a FRESH attack instead of coasting on its
                # pre-kill count, and count the reopen so no_reassert can cap the cycle.
                existing["status"] = "open"
                for kr in existing.get("kills", []):
                    if kr.get("accepted"):
                        kr["addressed"] = True
                existing["attempts"] = 0
                existing["reopens"] = int(existing.get("reopens") or 0) + 1
        else:
            led["claims"].append(norm)
            idx[norm["id"]] = norm
        accepted.append(norm["id"])
    return led, accepted, rejected


def record_kill(ledger: dict, target_id: str, by: str, kind: str, *, accepted: bool,
                round_no: int = 0) -> dict:
    """Record a kill attempt against a claim. An accepted kill sets ``killed``; a
    failed attempt increments ``attempts`` (which counts toward the target's survival)."""
    led = deepcopy(ledger)
    target = by_id(led).get(str(target_id))
    if target is None:
        return led
    target.setdefault("kills", []).append(
        {"by": by, "kind": kind, "round": round_no, "accepted": bool(accepted)}
    )
    if accepted:
        target["status"] = "killed"
    else:
        target["attempts"] = int(target.get("attempts") or 0) + 1
    return led


def add_contra_edge(ledger: dict, src_id: str, dst_id: str) -> dict:
    led = deepcopy(ledger)
    led.setdefault("edges", []).append([str(src_id), "contra", str(dst_id)])
    return led


def _stable_metamorph(receipt: dict) -> bool:
    """A stable ``metamorph`` receipt the CLERK wrote. One without ``by: clerk`` — a
    model-authored receipt, or one persisted before the tag existed — never counts: it is
    indistinguishable from a model certifying its own claim."""
    return (receipt.get("kind") == "metamorph" and bool(receipt.get("stable"))
            and receipt.get("by") == "clerk")


def _has_support(claim: dict) -> bool:
    """A claim is supported iff it carries a compoundable receipt — a verified ``cite``, a
    passing ``run``, or a clerk's stable ``metamorph``. ``unbacked`` receipts never support."""
    for r in (claim.get("receipts") or []):
        if not isinstance(r, dict):
            continue
        if r.get("kind") in ("cite", "run") and r.get("ok"):
            return True
        if _stable_metamorph(r):
            return True
    return False


def resolve(ledger: dict, *, k: int = DEFAULT_K) -> dict:
    """Recompute every claim's status from its recorded receipts and kills — the
    server-side control flow, never model self-report. ``killed`` if any UNADDRESSED
    accepted kill; else ``survived`` if supported AND it withstood >= k failed kill
    attempts; else ``open``. A kill addressed by a legal re-assert no longer counts, so a
    reopened claim can survive; a fresh accepted kill (unaddressed) kills again."""
    led = deepcopy(ledger)
    for c in claims(led):
        if any(kr.get("accepted") and not kr.get("addressed") for kr in (c.get("kills") or [])):
            c["status"] = "killed"
        elif _has_support(c) and int(c.get("attempts") or 0) >= k:
            c["status"] = "survived"
        elif c.get("status") != "killed":
            c["status"] = "open"
    return led


# --- reputation (session-scoped counters, tie-break only in v1) -------------------

def bump_rep(ledger: dict, agent: str, domain: str, field: str) -> dict:
    """Increment a per-``(agent, domain)`` counter (``kills`` / ``deaths`` / ``survives``)."""
    led = deepcopy(ledger)
    key = f"{agent}|{domain}"
    bucket = led.setdefault("rep", {}).setdefault(key, {"kills": 0, "deaths": 0, "survives": 0})
    if field in bucket:
        bucket[field] += 1
    return led


def rep_score(ledger: dict, agent: str, domain: str) -> int:
    b = reputation(ledger).get(f"{agent}|{domain}") or {}
    return int(b.get("kills", 0)) + int(b.get("survives", 0)) - int(b.get("deaths", 0))


def pick_assertor(ledger: dict, candidates: list[str], domain: str) -> str | None:
    """Highest domain rep_score asserts next; stable order breaks ties. Cheap
    reputation — the topology of who speaks moves with outcomes, not a YAML."""
    if not candidates:
        return None
    return max(candidates, key=lambda a: (rep_score(ledger, a, domain), -candidates.index(a)))


# --- predicates / compounding gate -----------------------------------------------

def compoundable(claim: dict) -> bool:
    """Only a supported, non-killed claim may move reputation or render as consensus.
    Default-deny: everything else is heard but cannot compound."""
    return isinstance(claim, dict) and claim.get("status") != "killed" and _has_support(claim)


def killed_ids(ledger: dict) -> list[str]:
    return [str(c["id"]) for c in claims(ledger) if c.get("status") == "killed" and c.get("id")]


def survived_ids(ledger: dict) -> list[str]:
    return [str(c["id"]) for c in claims(ledger) if c.get("status") == "survived" and c.get("id")]


def open_ids(ledger: dict) -> list[str]:
    return [str(c["id"]) for c in claims(ledger) if c.get("status") == "open" and c.get("id")]


def crucible(ledger: dict) -> list[dict]:
    """Open claims with no compoundable receipt — irreducibles the clerk cannot settle.
    The serializer must never render these as fact or consensus."""
    return [{"id": str(c.get("id")), "claim": c.get("claim")}
            for c in claims(ledger) if c.get("status") == "open" and not _has_support(c)]


def metamorph_only_ids(ledger: dict) -> list[str]:
    """Survived claims whose ONLY support is a stable ``metamorph`` — stability is not truth,
    so these are reported apart from cite/run-verified survivors."""
    out = []
    for c in claims(ledger):
        if c.get("status") != "survived":
            continue
        rs = [r for r in (c.get("receipts") or []) if isinstance(r, dict)]
        strong = any(r.get("kind") in ("cite", "run") and r.get("ok") for r in rs)
        meta = any(_stable_metamorph(r) for r in rs)
        if meta and not strong:
            out.append(str(c.get("id")))
    return out


def _signature(ledger: dict) -> tuple:
    return tuple(
        (str(c.get("id")), c.get("status"), int(c.get("attempts") or 0), len(c.get("receipts") or []))
        for c in claims(ledger)
    ) + (len(edges(ledger)),)


def is_fixpoint(prev: dict, curr: dict) -> bool:
    """True when a round produced no admissible change — no new claim, kill, receipt,
    or status flip. The loop stops here (or on budget)."""
    return _signature(prev) == _signature(curr)


# --- rendering (ledger -> prompt text / output block) ----------------------------

def render_ledger(ledger: dict) -> str:
    """The ledger as text for the next turn's prompt. Killed claims are shown with a
    do-not-reassert marker; unsettled/unbacked claims are quarantined under a header
    that forbids treating them as fact (default-deny in the prose, too)."""
    surv, killed, unsettled = [], [], []
    for c in claims(ledger):
        cid, text = str(c.get("id") or "?"), str(c.get("claim") or "").strip()
        if c.get("status") == "survived":
            surv.append(f"- [{cid}] {text}")
        elif c.get("status") == "killed":
            killed.append(f"- [{cid}] {text}  [KILLED — do not re-assert without a receipt addressing the kill]")
        else:
            tag = "" if _has_support(c) else "  [UNBACKED — heard, cannot compound]"
            unsettled.append(f"- [{cid}] {text}{tag}")
    parts = []
    if surv:
        parts += ["SURVIVED (receipt-backed):", *surv]
    if unsettled:
        parts += ["\nUNSETTLED (cannot be treated as fact):", *unsettled]
    if killed:
        parts += ["\nKILLED:", *killed]
    return "\n".join(parts) or "(empty ledger)"


def summary_block(ledger: dict) -> dict:
    """The ``json-falsify`` output block — the compact, machine-readable state."""
    return {
        "falsify_version": 1,
        "round": int(ledger.get("round") or 0),
        "status": str(ledger.get("status") or "active"),
        "survived": survived_ids(ledger),
        "killed": killed_ids(ledger),
        "open": open_ids(ledger),
        "crucible": [c["id"] for c in crucible(ledger)],
        "stable_unverified": metamorph_only_ids(ledger),
        "rep": reputation(ledger),
    }
