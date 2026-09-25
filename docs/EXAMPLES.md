# Real examples

These are real calls made to this server during everyday development work.
Questions and answers are trimmed for length; nothing is invented.

Each card follows the same shape: **what was asked**, **what the tool said**,
and **why it's a good demo of that mode**.

---

## Ask — one model, one sharp answer

### Validating JSON before or after auth?

**Asked:** Should the JSON payload be validated before or after the auth
middleware runs? *(one line — that's the whole question)*

**Answer (`ask`, Fable):**

> Auth first, then JSON validation — with one carve-out: a cheap, schema-free
> size/content-type gate in front of both. Validate-first turns your schema
> validator into an unauthenticated DoS surface and leaks schema via error
> messages. A common mistake: a framework's body-parser mounted globally at
> app level parses pre-auth even though validation is post-auth.

**Why it's a good demo:** a one-line question gets back the ordering, the
reasoning, the framework-level trap, and the exception case — in seconds.

### A second opinion on a Go process-handling plan

**Asked:** A plan to make Go's `cmd.Wait()` return as soon as the direct child
exits, when a backgrounded grandchild is holding the stdout pipe open.
"Critique this plan and flag failure modes."

**Answer (`ask_model(provider="kimi")`):** confirmed the plan's core mechanism, then caught a
cross-platform trap:

> `SetReadDeadline` works on Unix os.Pipe fds, but on Windows os.Pipe is
> backed by synchronous pipes and returns `os.ErrNoDeadline` — your drain
> needs a close-after-grace-timer fallback.

It also pointed out `cmd.WaitDelay`, a simpler stdlib primitive the caller had
missed entirely.

**Why it's a good demo:** the quick independent second opinion — homework
validated, platform landmine found, better primitive suggested.

---

## Council — several models answer, one synthesis reconciles them

### Sanity-checking a 66-branch refactor

**Asked:** "Sanity-check a refactor of a flat `if key == "x": ...` chain of 66
branches into a `dict[str, async handler]` registry, with no behavior change
except fixing one latent bug. Call out where you two DISAGREE."

**Answer (`ask_council`, Fable + MiniMax-M3):** the council found three ways
the "no behavior change" plan would silently change behavior:

- the new `None → 1` guard drops the old fall-through's `print("unknown")`;
- resolving aliases before dispatch means handlers that echo `key` now print
  the canonical form instead of the alias the user typed;
- moving fuzzy (`startswith`) branches behind the dict reorders precedence,
  accidentally resurrecting shadowed branches — a second, unintended "fix".

It also named the cheapest safety net: one static AST test asserting no
extracted handler reads a variable that used to be computed in the old
function's preamble.

**Why it's a good demo:** everyone has done this exact refactor. The council
caught bugs that type checks and casual review miss.

### Keeping reputation from silencing a correct minority

**Asked:** When weighting council members by reputation, how do I keep it
advisory so a low-reputation model that happens to be RIGHT is never
suppressed?

**Answer (`ask_council`, Fable + DeepSeek + Gemini):** the three labs proposed
three different mechanisms; the synthesis adjudicated:

> Never a hard filter. Don't feed "0.5" and hope the synthesizer ignores it —
> an LLM reads 0.5 as "coin flip" and penalizes. Route verification, not
> belief: name the low-reputation dissent as the first claim to send to the
> falsifier. Reputation becomes a scheduler for scrutiny rather than a filter
> on truth.

**Why it's a good demo:** real inter-lab disagreement, preserved under
`sources` and resolved with reasons — not blended into mush.

### A local LM Studio panel, synthesized by Fable

**Asked:** "In three short bullets: what is the most common cause of retry storms
between microservices, and how do you prevent them?"

**Answer (`ask_council(provider="lmstudio")`, default five-model local panel + Fable):** four of
the five locals answered (`qwen/qwen3.8-27b` hit the VRAM room check against a
resident model and reported an `unload_offer` instead of forcing a swap — quorum
4/5, `degraded: true`). Fable reconciled the four:

> **Cause:** synchronized retries — the "thundering herd." Fixed-interval (or
> unspaced) retries make every caller re-fire at the same moment, and each layer
> that retries (client, gateway, mesh) multiplies the wave.
> **Prevent:** exponential backoff with random jitter on every retry path; cap
> with a per-call limit or a retry budget...
> **Stop the bleeding:** circuit breakers, idempotency keys, rate limiting, load
> shedding, bulkheads.

**Why it's a good demo:** it runs entirely on your own GPU — each member loads,
answers, and is freed before the next (a single GPU serves one model at a time),
so a panel larger than VRAM still completes and the box ends as it started. Add
`synthesizer="lmstudio:<model>"` for a fully local panel, or pin your own members
with `models=[...]` / config `lmstudio_council`.

---

## Chain — draft → critique → decide

### Redis write-through vs cache-aside

**Asked:** What are the consistency pitfalls of a Redis write-through cache in
front of Postgres, and the most robust SIMPLE pattern without a distributed
lock?

**Answer (`ask_chain`, pipeline: MiniMax-M3 drafts → Fable critiques and
decides):**

> Final answer: drop write-through. Use cache-aside — write to Postgres,
> commit, then `DEL` the key — with TTL+jitter as the safety net.

The deciding stage adopted the draft's core idea but corrected three of its
details — including a generation-counter scheme that "doesn't actually close
the race it claims to close" (a check-then-act gap needing a short Lua
compare-and-set).

**Why it's a good demo:** you can *see* the pipeline working — a cheap model
does the legwork, a frontier model catches real errors in the draft, then
decides.

---

## Debate — two models argue, a third rules

### Lazy token bucket vs background refill task

**Asked:** For a per-user 100 req/min rate limiter: a background asyncio task
refilling buckets on a timer, or a "lazy" bucket storing only
`(tokens, last_refill)`? Pick ONE.

**Answer (`ask_debate`, resolution: adjudicated):**

> Use the lazy token bucket. Do not build the background refill task — the
> timer only approximates at tick granularity what the lazy design computes
> exactly.

The adversarial round also surfaced traps *neither* side opened with: a
100-per-min bucket actually permits ~199 requests in a worst-case rolling
minute, and TTL eviction alone doesn't bound memory.

**Why it's a good demo:** claim-by-claim rulings, then a decisive verdict with
concrete fixes — not "it depends."

### asyncio vs multiprocessing for an MCP server

**Asked:** For a Python MCP server running ~10 concurrent tool calls that each
shell out to a subprocess — asyncio with an executor, or a multiprocessing
pool? Pick one.

**Answer (`ask_debate`, resolution: adjudicated):**

> Pool-as-admission-control is dominated by `asyncio.Semaphore(N)`, which
> bounds concurrent subprocesses equally with zero extra processes — this
> closed the last live route by which the pool could win.

**Why it's a good demo:** a decisive verdict plus an implementation checklist
— and fittingly, the question is about an MCP server just like this one.

---

## Conference — models brainstorm together over rounds

### Stopping models from collapsing into agreement

**Asked:** How should a multi-model brainstorm tool keep its participants from
collapsing into easy agreement instead of genuinely diverging? *(Yes — the
tool critiquing its own design.)*

**What happened (`ask_conference`, Fable + DeepSeek, 2 rounds):** the two
models championed genuinely different fixes, and each attacked the other's
weakness:

- **Fable:** measure each answer's distance from the group's claim
  intersection — reward answers that diverge from the correlated prior.
- **DeepSeek:** that's gameable — a crank diverges without adding value.
  Instead, have each model *predict* what the others will say, and reward
  answers that surprise those predictions.

The closing report named this the crux — unresolved, with the experiments that
would settle it. One side's round-1 idea was adopted by the other in round 2.

**Why it's a good demo:** positions actually move across rounds, and the "map
of the disagreement" tells you what remains undecided and why.

---

## Falsify — claims must bring receipts

### Six bug claims from a review swarm, asserted then attacked

**Asked:** Six headline bug claims from an automated code review — assert each
as a typed claim and attack them.

**What happened (`ask_falsify`, asserter: MiniMax-M3, falsifier: Claude Opus
5):** six claims survived with verbatim-quote receipts. But the attack also
exposed confident-sounding claims built on nothing — including:

> The "zero auth middleware" evidence was a grep run *without* `-E`, so the
> `|` characters are literal and the pattern could never match — zero hits is
> fully explained by the pattern, not by the code.

**Why it's a good demo:** it catches a broken grep being used as proof of
absence. Separating claims-with-receipts from claims-that-sound-right is the
mode's entire pitch in one moment.

---

## Beyond software

### Diagnosing a house's water leak

**Asked:** Diagnose a recurring water-ingress problem in a new-build house,
given a five-month log of rainfall, wind gusts, and observed leaks. Rank the
causes with confidence levels; separate wind from rain volume; say what you
rule out. *(anonymized)*

**Answer (`ask_council` — also re-run through `ask_council(provider="ollama")` as a
cloud-vs-local comparison):**

> Defective dry-verge / gable-tray assembly delivering water to the wall-head,
> ~75–80% — the single physical bottleneck that explains why nine different
> wind directions all produce the same fixed internal wet spot. Wind-only
> control days: dry — condensation ruled out. Caveat: no moisture-meter
> readings exist; all fabric-state statements are inference.

**Why it's a good demo:** the same council that reviews Python refactors
produced a calibrated, evidence-weighted differential diagnosis — and flagged
the gap between correlation and proof on its own.
