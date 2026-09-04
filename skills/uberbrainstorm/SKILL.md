---
name: uberbrainstorm
description: "Invoke at the FUZZY FRONT END — when the task is 'what should we build and why', not yet 'how'. A design-first, approval-gated brainstorming ritual (after obra/superpowers) supercharged with the ask_fable council: oracles surface the clarifying questions you'd miss and generate genuinely divergent approaches, then a council red-teams the chosen design before it reaches the user. One question at a time, human holds the approval gate, NO implementation until the design doc is approved. Hands off to /uberplan."
---

# uberbrainstorm

Design-first brainstorming with a second brain. This is the **requirements + design** specialization of the uber family: `uberbrainstorm` (what/why) → `uberplan` (how) → `ubercode` (do it). It fuses obra/superpowers' disciplined, human-in-the-loop brainstorming (one question at a time, propose approaches, write a design doc, approval-gate implementation) with `ask_fable`'s multi-model council — so the *ideation* is cross-checked by several minds while the *judgment* stays with the user.

Usage: `/uberbrainstorm <feature / problem / behavior change>`

## When to use
- **USE for:** a new feature, component, or behavior change where the design is not yet settled; a vague or open-ended request; anything where the RIGHT thing to build is the hard part.
- **DON'T use for:** a decided task with one obvious approach (just do it, or `/uberplan` it); pure systems-architecture paradigm exploration where there is no human requirements loop (use `/uberarch`); writing the implementation plan for an *already-approved* design (use `/uberplan`).
- **Relationship to siblings:** `uberarch` fans abstract *architectural paradigms* to the oracles; `uberbrainstorm` is broader and earlier — it clarifies the problem WITH the user first, and may borrow uberarch-style council fan-out inside Phase 2 when the problem is architecture-heavy. It ENDS at an approved design doc and hands off to `uberplan`.
- **Cost:** Moderate. A few oracle turns (~120s each) interleaved with human turns. The council calls are the expensive part — reserve them for Phases 2 and 4.

## The two disciplines it refuses to drop
1. **One question at a time.** Never dump a questionnaire. Ask the single most decision-changing question, in multiple-choice form when you can, wait for the answer, then the next. The oracle may *generate* ten candidate questions; you ask the user one.
2. **The hard gate.** Do NOT invoke any implementation skill, write code, scaffold, or take any implementation action until you have presented a design AND the user has approved the written design doc. The only onward skill is `/uberplan`.

## Oracle ground rules (read before calling)
Same as `ubercode`/`uberplan`. The `ask_fable` oracles have NO tools — they reason only from `question` + `context`; paste the load-bearing facts. Key tools:
- **`ask(question, context, session, reset)`** — one model (Fable), multi-turn. Use for cheap single-shot helpers (question generation, a quick sanity read); **`ask_opus5`** is the identical tool on Claude Opus 5 at about half the price and faster, which is the better fit for exactly these helper turns. Name the `session` with a run-unique slug; `reset=true` on the first turn.
- **`ask_council(question, context, models?, tier?)`** — Fable + MiniMax-M3 (+ DeepSeek when its API key is configured) in parallel, Fable synthesizes; returns a merged `answer`, per-model `sources`, a `consensus` signal, and `material_disagreement`. Single-turn — carry the full brief in `context` every time. This is the divergent-approach engine (Phase 2) and the red-team (Phase 4). Escalate to `tier: middle`/`full` or explicit `models` ONLY for a foundational, hard-to-reverse decision.
- **`ask_chain(question, context, pipeline)`** — thread through an ordered pipeline, each model refining/criticizing the last. Use in Phase 2 when you want adversarial refinement rather than parallel divergence.
- **`ask_debate(question, context, proposer, opponent, adjudicator)`** — two models argue a claims ledger, a third adjudicates. The strongest Phase 4 red-team when the design hangs on one contested decision; heavier than a council, so reserve it for that case.
- **`context_pack(files=["path:START-END", ...])`** — let the server read real files into a bundle and pass its key as `context_ref` instead of pasting them into the brief.
- Word choice: the content guard whole-word-blocks a few security-loaded terms — notably **"payload"**; say "request body"/"JSON body" instead, and phrase questions in ordinary engineering language.
- Degrade gracefully: a refused/errored oracle turn is an assist that failed, never a gate. Fall back to your own reasoning and tell the user the council was unavailable.

## Procedure

### Phase 0 — Explore context (local)
Read the relevant files, docs, and recent commits. Distill a compact **landscape brief**: the core problem, the surrounding system, the hard constraints (perf/cost/compat/security), and the known unknowns. `context_write` it under a run key (e.g. `brainstorm-<slug>`) so every oracle turn can `context_ref` it instead of re-pasting.

### Phase 1 — Blind-spot questions (oracle-assisted, human-driven)
Before interrogating the user, optionally spend ONE cheap `ask` turn: *"Given this problem space, what are the load-bearing unknowns and the 5-8 clarifying questions whose answers most change the design?"* This surfaces questions you'd miss. Then YOU curate and ask the **user one question at a time** (multiple-choice when possible), focused on purpose, constraints, and success criteria. Update the brief on the bus as answers land.
- **Scope red flag:** if the request is really several independent subsystems ("chat + billing + analytics + storage"), say so immediately and propose decomposing into sub-projects before designing.

### Phase 2 — Divergent approaches (council)
Once purpose + constraints are clear, run ONE `ask_council` (or `ask_chain` for adversarial refinement) to generate **2-4 genuinely divergent approaches** with trade-offs — more diverse than any single model. Read the `sources` and `consensus`, then present **2-3 approaches to the user with a clear recommendation** and the trade-off that decides between them. This multi-mind divergence is the core reason to reach for uberbrainstorm over solo brainstorming.
- Force diversity: MVP-first vs. risk-first vs. clean-architecture; or optimize-for-latency vs. optimize-for-cost. Collapse near-duplicates rather than presenting them.

### Phase 3 — Converge (human)
The user picks or steers. Resolve the remaining decisions one question at a time. Apply **YAGNI ruthlessly** — cut every feature the success criteria don't demand. Prefer small, well-bounded units with clear interfaces.

### Phase 4 — Red-team the chosen design (council cross-check)
Before writing it up, run ONE `ask_council` to adversarially pressure-test the chosen approach: *"Stress-test this design. Failure modes, hidden coupling, YAGNI violations, what's missing, what's foundational/hard-to-reverse. Is it safe to commit as specified?"* Check `consensus`/`material_disagreement`; fold the surviving findings in. (Mirrors uberplan's cross-check on the riskiest candidate.)

### Phase 5 — Write the design doc + self-review (local)
Write to `docs/designs/YYYY-MM-DD-<slug>-design.md` (adapt to the project's docs convention). Sections scale to complexity — a few sentences for simple parts, 200-300 words for nuanced ones. Cover: problem & goal, chosen approach and **why it won over the rejected ones**, components & interfaces, data/control flow, error handling, testing, and open questions / known unknowns. Then **self-review**: scan for placeholders, contradictions, ambiguity, and scope creep; fix inline before showing the user.

### Phase 6 — User approves (HARD GATE)
Present the written doc and wait for explicit approval. Nothing downstream happens until the user approves. If they revise, loop back to the smallest phase that covers the change.

### Phase 7 — Handoff
On approval, hand off to **`/uberplan <the approved design>`** to turn the *what* into a concrete, judged implementation plan. Do not implement here — uberbrainstorm ends at an approved design.

## Best practices
- **Let the oracle brainstorm; let the human decide.** The council widens the option space and finds blind spots; the approval gate and the one-question rhythm stay with the user.
- **Ask for the negative space:** "What is the worst way to build this, and which parts of that are we secretly already doing?"
- **Keep the brief on the bus.** One `context_write`, many `context_ref` — don't re-paste the growing brief into every council turn.
- **Two council calls is the budget** (Phase 2 divergence + Phase 4 red-team). More than that means the problem wants `/uberarch`, or the design isn't converging and needs another human turn, not another model.
- **Name the run session** (`brainstorm-<slug>`) and `reset=true` the first `ask` turn so a prior run's context can't bleed in.
