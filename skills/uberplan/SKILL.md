---
name: uberplan
description: "Invoke for a big, ambiguous, or architecture-touching implementation task where you want more than one plan before committing — runs entirely locally, fans out N diverse candidate plans, and uses the ask_fable MCP (Fable — optionally cross-checked against MiniMax-M3 via ask_council) as an independent judge to score and stress-test them, then synthesizes locally into one final plan; skip for trivial one-liners."
---

# uberplan

Local multi-plan fan-out with Fable as the JUDGE, then LOCAL synthesis into one final plan. Runs entirely on the local agent; only the judging turns leave the machine (via the `ask_fable` MCP). Contrast with `/code-review ultra`, which runs in the cloud.

Usage: `/uberplan <task>`

## When to use
- USE for: big/ambiguous tasks, architecture or interface changes, risky refactors, anything where step ordering or hidden edge cases matter, or when the local agent runs on a weaker base model (DeepSeek/local) and wants a stronger reasoner to referee.
- DON'T use for: trivial one-liners, mechanical renames, or a change with one obvious approach. Just do those. uberplan's overhead (N plans + N+1 judge turns) is not worth it.
- Cost is honest, not instant: up to N+1 sequential judge turns at ~120s each. Budget for it.
- Companion: `ubercode` documents general `ask_fable` usage (oracle escalation + second opinion, and the `ask` / `ask_m3` / `ask_council` tools). uberplan is the PLANNING specialization; reuse ubercode's ground rules about the oracles.

## Oracle ground rules (read before calling)
- The `ask_fable` MCP exposes several ask tools; all return ONE JSON object. The oracles have NO tools — they reason ONLY from `question` + `context`. They cannot read files, run commands, or browse. YOU must paste the load-bearing code/structure/constraints into `context`.
  - **`ask(question, context="", context_ref=None, session="default", reset=false)`** — Fable, **multi-turn**. This is the judging workhorse (the comparative shared-session pattern below needs sessions).
  - **`ask_council(question, context="", context_ref=None)`** — Fable + **MiniMax-M3** (+ **DeepSeek** when its API key is configured — cheap direct models are preferred and consulted first) in parallel, then Fable synthesizes; returns a merged `answer` plus each raw take under `sources`. **Single-turn** (no session). Use it as a CROSS-CHECKED judge on the single highest-stakes candidate, or to referee your final synthesized plan — see step 3. For a heavier referee on the riskiest plan, pass a named `tier` (`middle` = +opus+glm+gemini+codex+grok+kimi, `full` = +the configured Ollama Cloud models) or add explicit `ollama:<model>` members via `models` — but only when the operator asked for a bigger cross-check (more oracles = more calls + slower).
  - **`ask_debate(question, context="", proposer=, opponent=, adjudicator=)`** — two models argue a claims ledger, a third adjudicates. Use it when two candidate plans are genuinely close and you want the strongest case for each rather than a blended verdict.
  - **`context_pack(files=["path:START-END", ...])`** — have the server read the real files into a bundle and pass its key as `context_ref`, instead of pasting the brief by hand.
  - **`ask_chain(question, context="", context_ref=None, pipeline="m3 > glm > deepseek > fable")`** — Ordered pipeline red-teaming. Use it to subject a plan to rigorous sequential critique where each stage refines the prior one to prevent anchoring.
  - **`ask_opus5(...)`** — `ask` on Claude Opus 5: half the price, faster, its own session namespace. Good for the bulk judging turns when you have many candidates.
  - **`ask_m3`**, **`ask_deepseek`**, **`ask_glm`**, **`ask_gemini`**, **`ask_codex`**, **`ask_grok`**, **`ask_kimi`**, **`ask_ollama`** — single-turn, single-model tools. A cheap independent second opinion on one plan (prefer the direct APIs `ask_m3`/`ask_deepseek`/`ask_glm` over pricier cloud models).
- Limits: `question` <= 65536 chars; **`context` is effectively unbounded** (no default cap; any operator-set cap is floored to ≥512,000 chars); ~240s/turn. Scope: engineering reasoning about software work (architecture, data/control flow, trade-offs, refactors, tooling). Refuses cyber/attack and non-software domains.
- Multi-turn (`ask` only): reuse the same `session` key for follow-ups. Fable keeps context server-side — DON'T re-send the brief or the transcript on later turns. `ask_chain`/`ask_m3`/`ask_council` are single-turn: each call is independent, so each must carry its own `context` (or `context_ref` to reuse a saved `context_write` block without repasting).
- Session hygiene: NEVER use `session="default"` — contexts bleed between runs. Name the session with a task slug. A key persists server-side across `/uberplan` runs, so start the opening `ask` turn with `reset=true` (dumps any prior transcript to a file, then starts empty) OR make the slug run-unique (e.g. `uberplan-<slug>-<timestamp>`). The "first turn carries the brief, later turns omit it" pattern below REQUIRES the session to start empty.
- Consuming a reply: on `status=ok`, read the `answer` field only (for `ask_council`, `answer` is the merged verdict; `sources.fable`/`sources.minimax` hold each raw take); ignore the `model`/`session` echoes.
- Response shapes: `{"status":"ok",...,"answer":"..."}` | council also adds `"mode":"council"`,`"consensus"`,`"synthesizer"`,`"sources"` | chain adds `"mode":"chain"`,`"stages"` | `{"status":"refused","stage":"guard"|"model","reason":"..."}` | `{"status":"error","kind":"timeout|sdk_error|binary_missing","detail":"..."}`. You may also see `needs_context` if Fable requests a file, or `context_ref_resolved`/`context_ref_missing`.

## Procedure

### 1. GATHER — build the planning brief
Scope the task locally: read the relevant files, list affected modules / interfaces / constraints. Distill a COMPACT "planning brief" — the facts every candidate plan must respect. This exact text becomes the judge's `context`.
- Cite `file:line`. Paste ONLY load-bearing snippets, not whole files.
- Include: the goal, the current relevant structure, hard constraints (APIs that can't change, invariants, perf/back-compat limits), and known unknowns.
- Context is effectively unbounded now, but keep the brief load-bearing anyway: a bloated brief buries the constraints and wastes the judge's attention. If it reads like a data dump, trim to what actually constrains the plans.

### 2. FAN OUT — generate N diverse plans (default N=3)
Produce N DIVERSE candidate plans from different angles. Either write them directly or dispatch parallel subagents. Suggested three angles:
- **A — MVP / smallest-diff-first**: minimal change that ships the outcome.
- **B — risk-first / hardening**: assume it breaks; sequence to de-risk (tests, migration, rollback, edge cases first).
- **C — clean-architecture / refactor-first**: fix the structure so the change is natural; accept a bigger diff.

Each plan states: goal, ordered steps, files touched, key trade-offs, risks.
- Diversity guard: if the N plans converge on the same approach, say so and collapse to the distinct ones — don't spend judge turns on near-duplicates.

### 3. JUDGE — ONE shared Fable session, comparative
Open ONE session key (e.g. `uberplan-authz`). Structure, generalized for any N: **one judge turn per candidate, then one synthesis turn (N+1 total).** The FIRST candidate turn carries the full brief in `context` AND `reset=true` to guarantee an empty session; EVERY later turn reuses the session with `context=""`.
- The plan + judging instructions go in `question` — cap 65536 chars. If a plan is longer, compress it to steps/files/trade-offs/risks. Do NOT paste code into `question`; the code is already in the brief on Turn 1.
- Comparative asks: the first candidate turn CANNOT ask "better/worse than the alternatives" — Fable has seen nothing yet. Only turns 2..N ask for that comparison.
- **Cross-check or Red-team option (recommended for the highest-stakes plan):** for the single riskiest candidate, ALSO run `ask_council(question=<that plan + judging ask>, context=<brief>)` for parallel verification, or `ask_chain(...)` for sequential red-teaming (e.g., getting a draft, heavily criticizing it without anchoring). You get independent scores reconciled into one verdict, and it flags where the reasoners disagree — exactly the plan you least want a single-model blind spot on.

Turn 1 (first candidate, with brief + reset):
```
ask(
  session="uberplan-authz",
  reset=true,
  context="""BRIEF — goal: move authz check out of each handler into one middleware.
Current: middleware.py:40 `def require_role(role)` decorator applied per-route (routes.py:12,55,88).
Constraint: public routes /health,/metrics must stay unauthenticated (routes.py:120).
Constraint: role list comes from JWT claim 'roles' (auth/jwt.py:33), cannot change token shape.
Invariant: 401 vs 403 semantics must be preserved (tests/test_authz.py:20).""",
  question="""Judge PLAN A (MVP/smallest-diff). Score 1-10 and give: flaws, missed edge cases, ordering hazards.
PLAN A:
1. Add global middleware that reads 'roles' claim and enforces a route->role map.
2. Delete per-route @require_role decorators.
3. Keep /health,/metrics on an allowlist.
Files: middleware.py, routes.py. Trade-off: central map can drift from routes. Risk: allowlist miss = lockout."""
)
```
Turns 2..N (remaining candidates): reuse `session="uberplan-authz"`, leave `context=""`, paste only that plan and ask the judge to score it AND say what it does better/worse than the plans already seen.

Final turn (synthesis): same session, `context=""`:
```
ask(session="uberplan-authz", question="Given all plans seen, what is the single best combined plan (which spine, which grafts) and what is the single biggest remaining risk?")
```

### 4. SYNTHESIZE — one final plan (LOCAL)
Synthesis is YOUR job, not the judge's — Fable only suggests a combination on the final turn. Take the winning plan's spine, graft the best ideas from the runners-up, and EXPLICITLY resolve every flaw flagged. Optionally, before presenting, run ONE `ask_council` or `ask_chain` on the final synthesized plan as a cross-checked sanity pass. Present to the user:
- Final ordered steps + files touched
- Risks and how each is mitigated
- Open questions / decisions needed
Do NOT auto-implement. uberplan ends at an agreed plan.

## Degrade gracefully — never block
The oracle is an assist, not a gate.
- `refused/stage=guard`: your `question` broke the 3–65536 char bound, or the input hit the denylist. Trim the `question`, remove any attack-shaped framing, retry once. (Context is effectively unbounded, so overflow is almost always the question, not the brief.) A denylist hit is **deterministic** — a verbatim resend refuses again, so reframe, don't retry: anchor the same engineering question to a concrete symbol + code in `context`. e.g. a plan framed around "how attacks exfiltrate every record" reframes to "review `BackupJob.run`'s write-then-rename for crash-safety; snippet: …".
- `refused/stage=model`: out of scope — reframe as pure software-engineering reasoning (tie the question to the symbol you're editing; non-software domains like biology/medicine/law and off-topic asks trip this), or skip judging that candidate. For `ask_council`, a model-stage refusal means BOTH oracles refused. The three refusal shapes and full ✗→✓ reframe playbook live in **ubercode's "Reframe, don't retry"** section; the same shapes apply to the judging turn here.
- `error/kind=timeout`: on Turn 1 (the only turn carrying the brief) trim the BRIEF; on later turns (`context=""`) trim the PLAN in `question`. Retry once; if it times out again, judge the remaining candidates and note the gap.
- `error/kind=binary_missing` or repeated `sdk_error`: the bridge is unavailable (`binary_missing` on `ask_m3`/`ask_council` specifically means the `mmx` CLI isn't installed — Fable via `ask` may still work). If Fable itself is down, SKIP judging entirely, present the N local plans plus your own synthesis, and tell the user "judge unavailable — plans un-refereed."

## Notes
- Context discipline is the #1 failure mode: an over-stuffed brief buries the constraints even though it now fits. Keep it load-bearing.
- Speed option: generate the N plans in parallel; keep judging in ONE shared `ask` session for comparative scoring (sequential), OR trade comparison for speed by fanning out independent single-turn judgments (`ask_m3`/`ask_council` are naturally single-turn for this). Default: shared Fable session. Core steps above stay harness-agnostic so opencode/DeepSeek agents can follow them verbatim.
