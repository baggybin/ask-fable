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

from dataclasses import dataclass, field

from . import oracles

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

_RAPPORTEUR_PROMPT = (
    "You are the rapporteur for the brainstorming conference below. Produce a MAP "
    "OF THE DISAGREEMENT — not a summary, not a tidy consensus. Structure it:\n"
    "CONVERGED — what the participants genuinely agreed on.\n"
    "THE CRUX — the one disagreement they could not resolve, and WHY it held.\n"
    "WHAT WOULD CHANGE IT — the specific fact or test that would settle it.\n"
    "Name positions concretely. If they never really disagreed, say so.\n\n"
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


async def run_conference(
    topic: str,
    model_keys: list[str],
    *,
    context: str = "",
    rounds: int = 3,
    synthesizer: str = "fable",
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
    posts: list[dict] = []

    # Round 1 — blind: everyone answers from the seed alone, then reveal together.
    blind_prompt = _BLIND_PROMPT.format(seed="\n\n".join(seed))
    blind = []
    for key in model_keys:
        blind.append((key, await oracles.run(key, blind_prompt)))
    for key, result in blind:
        if result.status == "ok" and (result.text or "").strip():
            who = oracles.label(key)
            text = result.text.strip()
            transcript.append(f"[{who} · round 1] {text}")
            posts.append({"model": who, "round": 1, "text": text})

    # Rounds 2..N — open: each model reads the running transcript and builds on it.
    for round_no in range(2, rounds + 1):
        for key in model_keys:
            prompt = _TURN_PROMPT.format(transcript="\n\n".join(transcript))
            result = await oracles.run(key, prompt)
            if result.status == "ok" and (result.text or "").strip():
                who = oracles.label(key)
                text = result.text.strip()
                transcript.append(f"[{who} · round {round_no}] {text}")
                posts.append({"model": who, "round": round_no, "text": text})

    map_text = ""
    if len(posts) >= 2:
        synth = await oracles.run(
            synthesizer, _RAPPORTEUR_PROMPT.format(transcript="\n\n".join(transcript))
        )
        if synth.status == "ok":
            map_text = (synth.text or "").strip()

    return ConferenceOutcome(
        topic=topic,
        rounds=rounds,
        models=[oracles.label(k) for k in model_keys],
        transcript=transcript,
        posts=posts,
        map_text=map_text,
    )
