"""Acceptance metrics for an ``ask_falsify`` ledger — does a run show *spark*, or is it
a retrieval test with the answer pre-loaded?

The conference that designed ``ask_falsify`` left one sharp acceptance bar: a demo where
the killing artifact is already in the context pack is a *grep test a dumb agent passes*,
not emergent reasoning. The real bar is a claim **whose answer nobody loaded** — the
system had to *manufacture* a discriminating probe. This module operationalizes that.

Pure functions over a ledger dict (see :mod:`falsify_ledger`). No I/O, no model calls — the
real-model runner lives in ``scripts/falsify_arena.py`` and calls in here.

Kill provenance is the v1-available proxy for the bar:
- ``self_refuted``  — a CHALLENGE killed a claim whose own ``cite`` was absent/fabricated.
  The easiest possible kill; the claim refuted itself. Pre-loaded.
- ``counter_cited`` — a CONTRA killed it with a quote from the pack. Still pre-loaded.
- ``constructed``   — a ``run`` or ``metamorph`` receipt killed it. Manufactured, not in
  the pack — this is the only kind that clears the bar (and it is a v2 receipt type).

So a v1 (cite/contra-only) run is EXPECTED to read "retrieval-bound"; the metric exists so
that when ``run:``/``metamorph:`` land you can *measure* whether the frontier gets probed.
The true difficulty-spread metric needs per-claim priors (v2 calibrated councils) and is
returned only when those priors are supplied.
"""

from __future__ import annotations

import statistics
from copy import deepcopy

from . import falsify_ledger as fl

_PRELOADED_KINDS = {"challenge": "self_refuted", "contra": "counter_cited"}
_CONSTRUCTED_KINDS = {"run", "metamorph"}


def _accepted_kills(ledger: dict) -> list[dict]:
    """Every kill the clerk accepted, flattened across claims."""
    out = []
    for c in fl.claims(ledger):
        for k in c.get("kills", []) or []:
            if isinstance(k, dict) and k.get("accepted"):
                out.append(k)
    return out


def kill_provenance(ledger: dict) -> dict:
    """Classify each accepted kill by how its evidence was obtained."""
    prov = {"self_refuted": 0, "counter_cited": 0, "constructed": 0, "total": 0}
    for k in _accepted_kills(ledger):
        kind = str(k.get("kind") or "challenge")
        if kind in _CONSTRUCTED_KINDS:
            prov["constructed"] += 1
        else:
            prov[_PRELOADED_KINDS.get(kind, "self_refuted")] += 1
        prov["total"] += 1
    return prov


def grep_ratio(ledger: dict) -> float | None:
    """Fraction of kills whose evidence was already in the pack. 1.0 = pure retrieval
    (a grep agent would pass); < 1.0 means some kill was manufactured. ``None`` if no
    kills occurred."""
    prov = kill_provenance(ledger)
    if prov["total"] == 0:
        return None
    return (prov["self_refuted"] + prov["counter_cited"]) / prov["total"]


def _rep_moves(ledger: dict) -> int:
    total = 0
    for bucket in fl.reputation(ledger).values():
        if isinstance(bucket, dict):
            total += sum(int(v) for v in bucket.values() if isinstance(v, (int, float)))
    return total


def spark_density(ledger: dict, turns: int) -> float:
    """deepseek's metric, adapted: committed signal per turn. A chatty run that commits
    nothing scores ~0 no matter how good it reads. signal = survived + killed + rep moves."""
    signal = len(fl.survived_ids(ledger)) + len(fl.killed_ids(ledger)) + _rep_moves(ledger)
    return signal / max(int(turns), 1)


def _attacked_ids(ledger: dict) -> list[str]:
    return [str(c["id"]) for c in fl.claims(ledger) if c.get("kills") and c.get("id")]


def difficulty_spread(ledger: dict, priors: dict[str, float] | None) -> float | None:
    """Spread (stdev) of the room's prior P(true) over the ATTACKED claims. Wide spread =
    attacks hit a range of difficulties (frontier probed); tight-and-high = only easy,
    consensus-obvious claims were attacked. Requires per-claim priors (v2 forced
    elicitation); ``None`` until those exist or with < 2 attacked claims."""
    if not priors:
        return None
    vals = [float(priors[cid]) for cid in _attacked_ids(ledger) if cid in priors]
    return statistics.pstdev(vals) if len(vals) >= 2 else None


def consensus_shadow(ledger: dict) -> dict:
    """The counterfactual with the clerk turned OFF — every asserted claim treated as
    ``survived`` and all kills dropped. This is what a "consensus = truth" machine would
    have committed; the ablation contrasts it with the real, receipt-gated run."""
    shadow = deepcopy(ledger)
    for c in shadow.get("claims", []):
        c["status"] = "survived"
        c["kills"] = []
    return shadow


def ablation_delta(real: dict, shadow: dict | None = None) -> dict:
    """What the clerk prevented: the claims it killed that an executor-off consensus run
    would instead have published as fact. A high ``prevented`` on a run whose ``shadow``
    would 'agree' is the demo that the cage is what produces the intelligence."""
    shadow = shadow if shadow is not None else consensus_shadow(real)
    clerk_killed = fl.killed_ids(real)
    return {
        "clerk_killed": sorted(clerk_killed),
        "consensus_would_accept": sorted(fl.survived_ids(shadow)),
        "prevented": len(clerk_killed),
    }


def report(ledger: dict, *, turns: int | None = None,
           priors: dict[str, float] | None = None) -> dict:
    """The full acceptance report + a one-word verdict.

    - ``no-falsification`` — nothing was killed; no evidence was tested.
    - ``retrieval-bound``  — every kill was pre-loaded (expected for v1 cite/contra).
    - ``frontier-probing`` — at least one kill was manufactured (a ``run``/``metamorph``
      receipt) — the bar is cleared."""
    prov = kill_provenance(ledger)
    gr = grep_ratio(ledger)
    if prov["total"] == 0:
        verdict = "no-falsification"
    elif prov["constructed"] == 0:
        verdict = "retrieval-bound"
    else:
        verdict = "frontier-probing"
    return {
        "verdict": verdict,
        "kill_provenance": prov,
        "grep_ratio": gr,
        "spark_density": spark_density(ledger, turns) if turns is not None else None,
        "difficulty_spread": difficulty_spread(ledger, priors),
        "ablation": ablation_delta(ledger),
        "survived": fl.survived_ids(ledger),
        "killed": fl.killed_ids(ledger),
        "crucible": [c["id"] for c in fl.crucible(ledger)],
        "note": (
            "retrieval-bound is EXPECTED for v1 (cite/contra only); the bar is cleared "
            "when a run:/metamorph: receipt manufactures a kill. difficulty_spread needs "
            "per-claim priors (v2 calibrated councils)."
        ),
    }
