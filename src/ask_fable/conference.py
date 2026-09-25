"""Brainstorm conference — models argue a topic over several rounds, hearing each
other, then a rapporteur writes the map of the disagreement.

This is the sequential cousin of ``ask_council``: where a council fans one
question out to isolated models and reconciles their separate answers, a
conference runs *turns* — each model reads the running transcript and adds to it,
so positions can actually move. The point is divergence, not a tidy average.

Model calls go through :func:`oracles.run`, so every backend ask-fable can reach
(fable/opus, deepseek/glm/minimax, atlas:/openrouter: tokens, …) can take a seat.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from . import oracles
from .oracle_common import OracleResult

# The default candidate pool offered by the picker / used when no models are
# given. Availability is checked before any seat is filled.
CANDIDATES: tuple[str, ...] = ("fable", "opus", "deepseek", "minimax", "glm")

_BLIND_PROMPT = (
    "You are opening a brainstorming conference on the topic below. No one has "
    "spoken yet — give YOUR own take, cold: the ideas, angles, and positions you "
    "think matter most, and stake out a clear stance. Be specific and concrete; do "
    "not hedge toward a safe middle.\n\n{seed}\n\nYour opening position:"
)

_TURN_PROMPT = (
    "You are a participant in a brainstorming conference with other AI models, "
    "arguing a topic together. Read the discussion so far, then add ONE "
    "contribution: a new idea, a concrete build on someone else's point, or a "
    "sharp disagreement where you genuinely disagree. Do not merely agree, and do "
    "not summarise — push the thinking somewhere it hasn't been. Be brief and "
    "specific.\n\nDISCUSSION SO FAR:\n{transcript}\n\nYour contribution:"
)

# The LAST open round. Same contribution contract, but the discussion is about to
# be mapped, so a participant that keeps circling adds nothing a rapporteur can
# use. Asking for the falsifier ("what would change my mind") is the one move that
# makes a held position auditable instead of merely stubborn.
_FINAL_TURN_PROMPT = (
    "You are a participant in a brainstorming conference with other AI models, "
    "arguing a topic together. This is the LAST round — a rapporteur will map the "
    "disagreement after it. Read the discussion so far, then land your position: "
    "state the ONE stance you hold and why, concede anything you now think was "
    "wrong, and finish with a single sentence naming the specific fact or test "
    "that would change your mind. Be brief and specific.\n\n"
    "DISCUSSION SO FAR:\n{transcript}\n\nYour closing position:"
)

# Phase 2 of the conference design — "attack the shared premise". The
# blind round produces N independent openings; what they all ASSUME is the part
# nobody will challenge, because challenging it is not what any of them was asked
# to do. Naming it and assigning one seat to argue it is false is the cheapest way
# to get a challenge to the thing everyone took for granted.
_PREMISE_PROMPT = (
    "Below are independent opening positions on one topic, written without seeing "
    "each other. Find the single most load-bearing claim that at least all but one "
    "of them ASSERT OR ASSUME — the shared premise the discussion is resting on, "
    "not a topic label. Reply with EXACTLY two lines and nothing else:\n"
    "CLAIM: <the shared premise, one sentence, stated as a claim that could be false>\n"
    "ASSERTED_BY: <comma-separated participant labels that assert or assume it>\n\n"
    "OPENING POSITIONS:\n{openings}\n\nYour two lines:"
)

_ATTACK_PROMPT = (
    "You are a participant in a brainstorming conference. Everyone's opening "
    "position rests on this shared premise:\n\n  {claim}\n\n"
    "Your job is the one nobody else will do: make the STRONGEST case that this "
    "premise is FALSE. Not a caveat, not 'it depends' — the real argument against "
    "it, and what follows for the topic if it does not hold. If after genuinely "
    "trying you believe the premise holds, say so plainly and give the best "
    "argument you could find anyway. Finish with one line:\n"
    "DISTINGUISHING TEST: <one observation whose result differs depending on "
    "whether the premise holds>\n\n{seed}\n\nYour case against the premise:"
)

_RAPPORTEUR_PROMPT = (
    "You are the rapporteur for the brainstorming conference below. Produce a MAP "
    "OF THE DISAGREEMENT — not a summary, not a tidy consensus. Structure it:\n"
    "CONVERGED — what the participants genuinely agreed on.\n"
    "THE CRUX — the one disagreement they could not resolve, and WHY it held.\n"
    "WHAT WOULD CHANGE IT — the specific fact or test that would settle it.\n"
    "Name positions concretely — by participant label AND by the claim itself, "
    "quoting it. If they never really disagreed, say so.\n\n"
    "TRANSCRIPT:\n{transcript}\n\nMap:"
)


@dataclass(slots=True)
class ConferenceOutcome:
    topic: str
    rounds: int
    models: list[str]
    transcript: list[str]
    posts: list[dict] = field(default_factory=list)
    map_text: str = ""
    # Seats (oracle keys) with at least one contribution — by key, since two seats
    # can share a label (fable / fable51).
    speakers: list[str] = field(default_factory=list)
    # Every turn that added nothing, and a failed map, as {"model", ["round"],
    # "status", "kind", "detail"} — so a silent seat or a missing map has a reason.
    errors: list[dict] = field(default_factory=list)
    map_error: dict | None = None
    map_thinking: str = ""
    # The shared premise found after the blind round and the seat assigned to argue
    # it is false: {"claim", "attacker", "asserted_by"}. Empty when not run.
    premise: dict = field(default_factory=dict)
    # `Participant N` -> the real model label, for the anonymized map. The
    # rapporteur names participants by pseudonym, so the reader needs the key.
    map_legend: dict[str, str] = field(default_factory=dict)


# How much transcript a turn (or the rapporteur) is shown. The transcript is
# rebuilt for EVERY turn, so an unbounded one grows quadratically in cost across a
# run and eventually outruns the smallest seat's context window.
_TRANSCRIPT_BUDGET = 60_000


def _render(entries: list[str], seeds: int, blind: int, budget: int | None = None) -> str:
    """The transcript as a turn should see it, within ``budget`` characters.

    The seed and the whole BLIND round are always kept: the blind round is the only
    independent evidence in the run — the thing commit-then-reveal exists to
    produce — so dropping a seat from it silently changes what "blind" means. Open
    rounds are kept newest-first, because a turn argues with what was just said.
    What falls out is the middle, and it says so.
    """
    # Read at call time, not bound as a default: a default argument freezes the
    # module constant at import, so an override could never take effect.
    budget = budget or _TRANSCRIPT_BUDGET
    head, rest = entries[: seeds + blind], entries[seeds + blind :]
    kept: list[str] = []
    spent = sum(len(x) + 2 for x in head)
    for line in reversed(rest):
        if spent + len(line) + 2 > budget and kept:
            break
        kept.append(line)
        spent += len(line) + 2
    kept.reverse()
    elided = len(rest) - len(kept)
    if elided:
        kept.insert(0, f"[… {elided} earlier turns elided to fit the transcript budget …]")
    return "\n\n".join(head + kept)


def _parse_premise(text: str) -> tuple[str, list[str]]:
    """``(claim, asserted_by)`` from the premise turn, or ``("", [])``.

    Deliberately forgiving about everything except the CLAIM line: a missing or
    empty claim means there is nothing to attack, and the round is skipped rather
    than an invented premise being handed to a model."""
    claim, asserted = "", []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not claim and stripped.upper().startswith("CLAIM:"):
            claim = stripped.split(":", 1)[1].strip()
        elif stripped.upper().startswith("ASSERTED_BY:"):
            asserted = [p.strip() for p in stripped.split(":", 1)[1].split(",") if p.strip()]
    return claim, asserted


def _anonymize(
    entries: list[str], seeds: int, speakers: list[str], rapporteur: str,
    line_keys: list[str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The transcript with every speaker replaced by ``Participant N``.

    The rapporteur is normally ALSO a participant — the default synthesizer sits on
    the default bench — so it was writing the map of an argument it had taken part
    in, with everyone named. The council solved exactly this for its synthesizer
    (anonymize, and read your own answer last); the conference never inherited it.
    Its own seat gets the highest number, so it reads its own voice last.

    Only the rapporteur's copy is anonymized: ``outcome.transcript`` and ``posts``
    keep the real labels, and the legend is returned so a reader can map back. The
    map text itself is never string-substituted — that is surgery on model prose.
    """
    ordered = [k for k in speakers if k != rapporteur] + [k for k in speakers if k == rapporteur]
    # Keyed on the oracle KEY, never the label: `fable` and `fable51` share a label,
    # as do `atlas:X` and `openrouter:X` (a mix the docs recommend). Keyed on the
    # label they collapsed into one pseudonym, so the rapporteur read two
    # independent openings as one participant contradicting itself, and the legend
    # reported fewer participants than there were seats.
    alias = {key: f"Participant {i + 1}" for i, key in enumerate(ordered)}
    legend = {pseudo: oracles.label(key) for key, pseudo in alias.items()}
    out = list(entries[:seeds])  # TOPIC / SHARED CONTEXT pass through unchanged
    for i, line in enumerate(entries[seeds:], start=seeds):
        # Rewrite by the SEAT that wrote the line, not by matching its label text:
        # two seats sharing a label are indistinguishable to a substitution, so one
        # of them would keep its real name (leaking it) or both would collapse into
        # one pseudonym (making two independent openings look like one participant
        # contradicting itself).
        key = line_keys[i] if line_keys and i < len(line_keys) else None
        pseudo = alias.get(key or "")
        if pseudo and line.startswith("["):
            out.append(f"[{pseudo} · " + line.split(" · ", 1)[1] if " · " in line else line)
        else:
            out.append(line)
    return out, legend


def _failure(who: str, result: OracleResult, round_no: int | None = None) -> dict:
    """A turn that produced nothing usable: who, when, and why."""
    entry: dict = {"model": who}
    if round_no is not None:
        entry["round"] = round_no
    entry.update(
        status=result.status,
        kind=result.kind or ("empty_answer" if result.status == "ok" else result.status),
        detail=(result.text or "").strip() or "no text returned",
    )
    return entry


async def _fan_out(
    model_keys: list[str], prompt: str, max_parallel: int | None
) -> list[tuple[str, OracleResult]]:
    """Ask every seat the SAME prompt at once, and return the answers in
    ``model_keys`` order.

    Completion order is deliberately discarded: the transcript is the record of
    the conference, so which seat is committed first must be a property of the
    bench, not of whichever backend happened to be fastest that run.

    Calls go through :func:`oracles.run_bounded` rather than ``run`` so a seat
    cancelled while still QUEUED is recorded as a call that reached no backend,
    instead of being charged a latency sample it never spent."""
    sem = asyncio.Semaphore(max(1, max_parallel or len(model_keys) or 1))
    tasks = [
        asyncio.create_task(oracles.run_bounded(sem, key, prompt), name=f"conference:{key}")
        for key in model_keys
    ]
    try:
        await asyncio.wait(tasks)
    except BaseException:
        # `asyncio.wait` does not cancel what it was given, so a cancellation here
        # would otherwise leave every seat running with nobody waiting on it.
        for task in tasks:
            task.cancel()
        raise
    out: list[tuple[str, OracleResult]] = []
    for key, task in zip(model_keys, tasks, strict=True):
        exc = task.exception()
        if exc is None:
            out.append((key, task.result()))
            continue
        if not isinstance(exc, Exception):  # BaseException: never swallow
            raise exc
        out.append(
            (key, OracleResult("error", kind="sdk_error", text=f"{type(exc).__name__}: {exc}"))
        )
    return out


async def run_conference(
    topic: str,
    model_keys: list[str],
    *,
    context: str = "",
    rounds: int = 3,
    synthesizer: str = "fable",
    max_parallel: int | None = None,
    attack_premise: bool = True,
) -> ConferenceOutcome:
    """Round 1 is BLIND — each model answers the topic alone (no peer transcript),
    the answers are committed, then revealed together; from round 2 the models read
    the running transcript and argue. Finally ``synthesizer`` writes the map. The
    commit-then-reveal stops a later speaker from autocompleting an emerging
    consensus instead of forming its own position."""
    seed: list[str] = [f"TOPIC: {topic}"]
    if context.strip():
        seed.append(f"SHARED CONTEXT:\n{context.strip()}")
    transcript: list[str] = list(seed)
    # The seat behind each transcript entry, index-aligned (None for the seed), so
    # the rapporteur's copy can be anonymized by SEAT rather than by label text.
    line_keys: list[str | None] = [None] * len(seed)
    posts: list[dict] = []
    speakers: list[str] = []
    errors: list[dict] = []

    def take(key: str, result: OracleResult, round_no: int, role: str = "") -> None:
        """Put a turn on the transcript, or record why it added nothing."""
        who = oracles.label(key)
        text = (result.text or "").strip() if result.status == "ok" else ""
        if not text:
            errors.append(_failure(who, result, round_no))
            return
        tag = f" · {role.replace('_', ' ')}" if role else ""
        transcript.append(f"[{who} · round {round_no}{tag}] {text}")
        line_keys.append(key)
        post: dict = {"model": who, "round": round_no, "text": text}
        if role:
            post["role"] = role
        # A turn the provider cut off at its output cap comes back `status="ok"`
        # with `kind="truncated"`; without this the post reads as a finished
        # contribution, and the next round argues against half a position.
        if getattr(result, "kind", "") == "truncated":
            post["partial"] = True
        posts.append(post)
        if key not in speakers:
            speakers.append(key)

    # Round 1 — blind: everyone answers from the seed alone, then reveal together.
    # CONCURRENT, because blind means no participant can see another: serializing
    # it bought nothing but wall-clock, and the whole run was models x rounds + 1
    # calls in series. Rounds 2..N below must stay strictly sequential — reading
    # the running transcript is the entire point of an open round.
    blind_prompt = _BLIND_PROMPT.format(seed="\n\n".join(seed))
    for key, result in await _fan_out(model_keys, blind_prompt, max_parallel):
        take(key, result, 1)

    # Phase 2 — attack the shared premise. Costs exactly two calls whatever the
    # bench size: one to NAME what everyone assumed, one to argue it is false.
    # Needs >= 3 seats ("all but one of them" is meaningless with two) and at least
    # one open round for the attack to land in.
    premise: dict = {}
    if attack_premise and rounds >= 2 and len(speakers) >= 3:
        anon_open, legend = _anonymize(
            transcript, len(seed), speakers, synthesizer, line_keys
        )
        found = await oracles.run(
            synthesizer, _PREMISE_PROMPT.format(openings="\n\n".join(anon_open[len(seed) :]))
        )
        claim, asserted = _parse_premise(found.text if found.status == "ok" else "")
        if not claim:
            errors.append(
                {
                    # No `round`: `errors` is read as "which seat fell silent when",
                    # and the synthesizer spoke normally — it just found no premise.
                    "model": oracles.label(synthesizer),
                    "stage": "premise",
                    "status": found.status,
                    "kind": "premise_unavailable",
                    "detail": (found.text or "").strip()[:300]
                    or "no CLAIM: line in the premise reply",
                }
            )
        else:
            # Prefer a seat that did NOT assert it; when they all did — the usual
            # case, and the interesting one — take the last, so the attacker is not
            # habitually the first speaker or the rapporteur.
            asserted_real = {legend[a] for a in asserted if a in legend}
            # An empty or unresolvable ASSERTED_BY ("all of them", or no line at
            # all — `_parse_premise` allows it) means we learned nothing about WHO
            # asserted the claim, not that nobody did: the claim was selected for
            # being shared. Falling through to `speakers[0]` there handed the
            # attack to the first speaker, which by default is also the rapporteur
            # — the two seats this is meant to avoid.
            attacker = (
                next((k for k in speakers if oracles.label(k) not in asserted_real), speakers[-1])
                if asserted_real
                else speakers[-1]
            )
            # The SEED, not the bare topic: every other turn sees the shared
            # context, and an attacker arguing about code it cannot see is the
            # very defect this release removes from the council synthesizer. It
            # also keeps the prompt distinct per codebase, so `oracles.run`'s
            # cache cannot serve one conference's attack to another.
            attack = await oracles.run(
                attacker, _ATTACK_PROMPT.format(claim=claim, seed="\n\n".join(seed))
            )
            take(attacker, attack, 1, role="premise_attack")
            premise = {
                "claim": claim,
                "attacker": oracles.label(attacker),
                "asserted_by": sorted(asserted_real),
            }

    blind_entries = len(transcript) - len(seed)

    # Rounds 2..N — open: each model reads the running transcript and builds on it.
    for round_no in range(2, rounds + 1):
        # Rotate the speaking order. It was fixed, so the first seat never once
        # argued with peer context and the last always had the most — a standing
        # advantage that had nothing to do with what any of them thought. A pure
        # function of the round index, so a replay is still deterministic.
        offset = (round_no - 1) % len(model_keys)
        order = model_keys[offset:] + model_keys[:offset]
        template = _FINAL_TURN_PROMPT if round_no == rounds else _TURN_PROMPT
        for key in order:
            prompt = template.format(transcript=_render(transcript, len(seed), blind_entries))
            take(key, await oracles.run(key, prompt), round_no)

    map_text, map_thinking, map_error = "", "", None
    map_legend: dict[str, str] = {}
    if len(posts) >= 2:
        anon, map_legend = _anonymize(
            transcript, len(seed), speakers, synthesizer, line_keys
        )
        rendered = _render(anon, len(seed), blind_entries)
        if "earlier turns elided" in rendered:
            # The caller sees the WHOLE argument in `transcript`, so a map written
            # on less than that has to say so, or "the one disagreement they could
            # not resolve" reads as a claim about turns the rapporteur never saw.
            errors.append(
                {
                    "model": oracles.label(synthesizer),
                    "stage": "map",
                    "status": "ok",
                    "kind": "transcript_elided",
                    "detail": "the map was written on a budget-trimmed transcript; the "
                    "blind round and the most recent turns were kept in full",
                }
            )
        synth = await oracles.run(synthesizer, _RAPPORTEUR_PROMPT.format(transcript=rendered))
        map_text = (synth.text or "").strip() if synth.status == "ok" else ""
        if map_text:
            map_thinking = synth.thinking
        else:
            map_error = _failure(oracles.label(synthesizer), synth)

    return ConferenceOutcome(
        topic=topic,
        rounds=rounds,
        models=[oracles.label(k) for k in model_keys],
        transcript=transcript,
        posts=posts,
        map_text=map_text,
        speakers=speakers,
        errors=errors,
        map_error=map_error,
        map_thinking=map_thinking,
        map_legend=map_legend,
        premise=premise,
    )
