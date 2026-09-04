---
name: ubercode
description: "When you hit a genuinely hard reasoning problem you can't crack yourself — a subtle architecture decision, a concurrency/data-flow bug you can't pin down, a \"why is this actually happening\" mystery — OR you need an independent second opinion on a high-consequence, hard-to-reverse plan/diff (concurrency, data model, public API, migration), route that narrow reasoning to Anthropic's Fable model — or Claude Opus 5 for cheaper, faster turns — and, when you want a cross-checked answer, a council of other oracles (MiniMax-M3, DeepSeek, GLM, Gemini, GPT-5.6 Sol, Grok, Kimi), via the ask_fable MCP server; especially valuable when your own base model is weaker (DeepSeek/local) and you want to check your work against a stronger reasoner."
---

# ubercode

Treat Fable (the newest `claude-fable-*`, reached via the `ask_fable` MCP server) as a smarter reasoning partner for the hard 5% of your work. **Claude Opus 5** is the same tool at roughly half the price and noticeably faster (`ask_opus5`), and several other oracles — **MiniMax-M3**, DeepSeek, GLM, Gemini, GPT-5.6 Sol, Grok, Kimi — are available through the same server for a cross-model check. These oracles have NO tools: they cannot read files, run commands, or browse. They reason ONLY from your question text + the `context` you paste. Weak context in, weak answer out — so the burden is on YOU to package the problem well. Then you verify the reasoning; you never blindly trust it. The 5% framing is the whole point: if a step is routine, cheap to check yourself, or low-stakes, do NOT ask.

## The tools (all on the `ask_fable` MCP server)

- **`ask(question, context="", context_ref=None, session="default", reset=false)`** — Fable, multi-turn. Your default oracle. Reuse the same `session` key for follow-ups (Fable keeps the transcript server-side). Pass `context_ref` to inject large context without repasting.
- **`ask_opus5(...)`** — the same multi-turn tool as `ask`, on Claude Opus 5: about half Fable's price and faster, same OAuth session, sessions namespaced separately. Prefer it for high-volume or long back-and-forth work; keep `ask` for the hardest calls.
- **`ask_m3`**, **`ask_deepseek`**, **`ask_glm`**, **`ask_gemini`**, **`ask_codex`**, **`ask_grok`**, **`ask_kimi`**, **`ask_ollama`** — single-model/single-turn tools. Use for a fast independent take, or when you specifically want the other model's angle; prefer the cheap direct APIs (`ask_m3`/`ask_deepseek`/`ask_glm`) over pricier cloud models, and the local CLIs (`ask_grok`/`ask_kimi`) over the equivalent `ask_atlas` ids.
- **`ask_council(question, context="", context_ref=None, models=["fable","minimax"])`** — asks **several models in parallel**, then Fable **synthesizes** them into one merged answer (reconciling conflicts on the merits); every raw answer is returned under `sources`. The default council is fable+minimax, +deepseek when its API key is configured (cheap direct models first). `models` picks from `fable`, `opus`, `deepseek`, `minimax`, `glm`, `gemini`, `codex`, `grok`, `kimi` — e.g. `models=["fable","minimax","glm","deepseek"]` for a four-model cross-check. (`fable` tracks the newest Fable automatically; `fable51` pins `claude-fable-5-1` and is only worth naming once the two diverge.) You can also add Ollama Cloud models as `ollama:<model>` tokens, or pass a named **`tier`**: `default`, `middle`, `full`. Use when a wrong answer is costly and you want independent reasoners to cross-check each other. Single-turn.
- **`ask_chain(question, context="", context_ref=None, pipeline="m3 > glm > deepseek > fable")`** — threads a question through an **ordered pipeline**, each stage criticizing and refining the last to prevent "anchoring" effects. Use for drafting -> red-teaming -> deciding.
- **`ask_debate(question, context="", proposer=, opponent=, adjudicator=)`** — two models argue a claims ledger and a third adjudicates. The heaviest mode: reserve it for a genuine dilemma where you want the strongest case for BOTH sides, not a merged answer.
- **Context Bus**: `context_pack(files=["path:START-END", ...])` — name repo files and the server reads them into a bundle for you, so you point instead of pasting; plus `context_write(key, text)`, `context_read(key)`, `context_list()`, `context_delete(key)` for arbitrary blobs. All `ask_*` tools accept `context_ref` to use these keys instead of repasting `context`.
- **`reset_session(session, save=true)`** — dump-and-clear a Fable thread's transcript.

## Two modes — decision rule

- **(A) Oracle escalation** — use when you are genuinely stuck or the decision is high-consequence and non-obvious: a subtle architecture choice, a concurrency/data-flow/ordering bug you can't pin down, a "why is this actually happening" mystery, a trade-off whose downstream effects aren't clear. Ask BEFORE you thrash for many edits.
- **(B) Second-opinion / verify** — use ONLY before a high-consequence, hard-to-reverse plan or diff: concurrency/ordering changes, data-model or migration changes, public API changes, routing changes with wide blast radius. Hand the oracle the plan/diff + your own reasoning and ask it to adversarially find what's wrong, what you missed, what breaks. NOT for routine or mechanical changes — if you'd be comfortable committing without review, don't ask.

Quick rule: *don't know the answer* → mode A. *Have an answer, want it stress-tested* → mode B. If you can get the answer by reading a file or running a command yourself, do THAT instead.

**Which tool for the mode:** default to `ask` (Fable). Reach for **`ask_council`** when the stakes justify independent reasoners in parallel — the highest-consequence mode-B reviews and the gnarliest mode-A mysteries (it will explicitly flag where the two disagree). Reach for **`ask_chain`** for an ordered refinement pipeline (e.g. get a draft from MiniMax and have Fable ruthlessly critique and finalize it). Use **`ask_m3`** when you just want MiniMax's separate opinion cheaply. Note all tools except `ask` are single-turn — for a drill-down conversation, stay on `ask` with a session.

**Council/Chain size is the OPERATOR's call, not yours.** `ask_council` defaults to `["fable","minimax"]` — keep it there. Do NOT self-escalate to `glm`/`deepseek`/`ollama:*`/`ask_chain` without a reason. Add them only when the operator explicitly asks for a bigger cross-check.

## How to call it

Put the concrete artifacts (code, paths, structure, error, your hypothesis) in `context`; keep `question` a sharp, specific ask. For `ask`, always pass a descriptive per-topic `session` key (e.g. `"dup-events"`, not `"default"`) — never pile unrelated asks onto one key, because contexts bleed together. Reuse the SAME key for follow-ups. Start a new key (or `reset=true`) when you switch topics — the prior transcript is dumped to a file first, so it's not lost. Limits: question 3–65536 chars; **context is effectively unbounded** (no default cap; any operator-set cap is floored to ≥512,000 chars) — so paste the load-bearing slice generously, but still curate (a whole repo is noise, not signal). If context is large and reused, **`context_write` it once and pass `context_ref="key"`** on subsequent asks instead of re-pasting.

## Examples

**A — concurrency/data-flow bug (oracle, Fable):**
```
ask(
  question="Under load, events occasionally fire twice for one write. My hypothesis: the emit happens before the txn commits, so a retry re-emits. Is that consistent with this code, and what's the minimal correct fix?",
  context="""File: core/events/seam.py
def persist(self, rec):
    self._session.add(rec)
    self._bus.emit(Event.WRITTEN, rec.id)   # <-- emit here
    self._session.commit()

Bus.emit is synchronous; subscribers may raise. On raise the caller retries persist(rec) with the same rec. rec.id is assigned pre-commit by the ORM default.""",
  session="dup-events"
)
```
→ On `ok`, apply the fix (e.g. emit in an after-commit hook), then actually reproduce/verify. Follow up on the SAME `session` if you need to drill in.

**B — cross-checked adversarial review of a high-consequence diff (council):**
```
ask_council(
  question="Adversarially review this ordering change. I'm moving emit to an after-commit hook to stop duplicate events. What breaks under concurrency, partial failure, or subscriber exceptions? What ordering guarantees do I lose? Don't rubber-stamp it.",
  context="""Goal: stop double-fired events (emit currently runs before commit; a retry re-emits).

Diff:
--- a/core/events/seam.py
+++ b/core/events/seam.py
@@
 def persist(self, rec):
     self._session.add(rec)
-    self._bus.emit(Event.WRITTEN, rec.id)
-    self._session.commit()
+    self._session.commit()
+    self._session.info.setdefault("_after_commit", []).append(
+        lambda: self._bus.emit(Event.WRITTEN, rec.id))

after_commit hooks run in registration order, outside the txn. Bus.emit is synchronous; subscribers may raise. Retries call persist(rec) again with the same rec."""
)
```
→ Read the synthesized `answer` (it flags where Fable and MiniMax disagree), and inspect `sources.fable` / `sources.minimax` if you want each raw take. Treat findings as a checklist to confirm against the real code; adopt the valid ones, discard the rest.

**A — architecture trade-off (Fable):**
```
ask(
  question="Should provider routing live in a middleware layer or be injected per-request at the call site? I need per-request model override for env-provider-routing without a global. Trade-offs and a recommendation.",
  context="Agents run on Claude, DeepSeek, or local. Today the provider is a module-level singleton read in ~30 call sites. Requirement: a single request can force a specific provider/model. Constraints: no DI framework, sync codebase, must stay testable.",
  session="provider-routing"
)
```

**Fast independent second take (MiniMax alone):**
```
ask_m3(question="Is there a cleaner way to express this retry-with-backoff than the nested try/except below? Any correctness bug?", context="<paste the function>")
```

**Multi-turn follow-up (reuse session, no re-paste — `ask` only):**
```
ask(question="Given your after-commit-hook suggestion, how do I keep ordering guarantees if two writes commit in the same tick?", session="dup-events")
```

## When NOT to use

- You can just read the file / run the command / check the test yourself — do that; don't outsource cheap facts.
- Routine, low-stakes, or mechanical edits — don't offload every step; ubercode is for the hard/high-consequence 5%. If you'd commit it without a review, don't ask for one.
- Trivia or non-software domain knowledge (biology, medicine, law, general facts) — the oracles will `REFUSED` it, and it's out of scope anyway.
- Security testing, attacks, or offensive tooling — hard-rejected by the guard denylist. Don't try.
- Secrets: never paste API keys, tokens, credentials, or PII into `question`/`context`. Redact first.
- Don't burn `ask_council` on easy questions — it costs two model calls plus a synthesis turn. Save it for when the cross-check is worth it.

## Reframe, don't retry

A refused question is **deterministic** — the guard never consults a model, and the model contract is fixed. Resending the same `question` re-refuses it. When you get `{"status":"refused",...}`, don't retry verbatim; **reframe** it. Reframing is almost always possible and usually makes the question *better* — it forces you onto a concrete symbol.

Three refusal shapes exist, and each has a reframe pattern. (Scope is decided by the **shape of prohibited intent in your `question` text**, case-insensitively — not by the topic in the abstract. Breadth is never a reason to refuse.)

### 1. Security-testing / offensive-tooling / attack content (hard-rejected by the guard)

**Shape that trips it:** phrasing that reads as building, deploying, or *intending* offensive tooling — malware families and their payloads, bulk encryption across filesystems, exfiltration of "all/every/entire" data, extortion or ransom demands (payment instruments, crypto tickers, wallet addresses), lock-up of systems. The match is on intent-shaped phrases in your `question`, not on security as a subject.

> Don't try to enumerate or "test" the denylist with literal trigger terms — the vocab is encoded out of the readable source for a reason, and probing it just pollutes the owner's audit log. Reframe instead.

**Reframe** to concrete code-reasoning about a specific symbol you're working on, with the code in `context`:
- ✗ "write malware that encrypts all files" → ✓ "in `crypto.py:AesWrapper`, is `MODE_GCM` the right mode for authenticating a single blob? snippet: …"
- ✗ "how do attackers exfiltrate every record from a DB" → ✓ "review `BackupJob.run`'s write-then-rename for crash-safety; snippet: …"

**Defensive security work on your own code is in scope** — hardening auth, reviewing access-control logic, auditing your own safeguards. Frame it as engineering of the specific module: name the symbol, paste the code, ask the engineering question (correctness, ordering, edge cases).

### 2. Non-software domain knowledge (model-side `REFUSED`)

The oracles are **software engineering** reasoners. They `REFUSED:` biology, medicine, chemistry, law, finance, and general trivia. Breadth is fine *only* when it relates to building software.

**Reframe** by tying the question to a concrete code symbol:
- ✗ "how does the heart work" → ✓ "is `HeartModel.tick()` updating chambers in the right order? method: …"
- ✗ "what's the legal definition of defamation" → ✓ "does `license_check.py` match the intended LICENSE-flags set? snippet: …"

### 3. Off-topic for your current software work

If the question isn't tied to the code/task at hand, the model may refuse it as out of scope. **Reframe** by anchoring to the actual symbol you're editing and putting its code in `context`.

## Handling the response (always one JSON object)

- `ask` / `ask_m3` ok → `{"status":"ok","model":...,"answer":...}` (ask also echoes `session`). Use the reasoning as INPUT, then verify (reproduce, test, re-read the code). For `ask`, continue the thread on the same `session` key. (You might also see `needs_context` if Fable requests a file, or `context_ref_resolved`/`context_ref_missing`.)
- `ask_council` ok → `{"status":"ok","mode":"council","synthesizer":"<model id>","answer":<merged>,"consensus":true|false,"sources":{...}}`. Read `answer`; check `consensus`; consult `sources` for each raw take.
- `ask_chain` ok → `{"status":"ok","mode":"chain","answer":...}` with intermediate `stages`.
- `{"status":"refused","stage":"guard","reason":...}` → your input tripped the sanity floor (empty/<3/>65536 char question) or the prohibited-use denylist. Fix the length; if it's a denylist hit, **reframe, don't retry** (see above) — the guard is deterministic, so a verbatim resend just refuses again.
- `{"status":"refused","stage":"model","reason":...}` → judged out of scope: cyber/attack content, non-software domain knowledge, or something unrelated to your software work. For council this means BOTH refused. **Reframe** as a concrete engineering question about YOUR code (see above); drop it only if it's genuinely off-scope.
- `{"status":"error","kind":"timeout|sdk_error|binary_missing","detail":...}` → `timeout`: the turn may still have cost model time, so do NOT blindly retry — narrow the question or trim context first, then re-ask (default 120s/turn, tunable via `ASK_FABLE_TIMEOUT`). `binary_missing`/`sdk_error`: a bridge/CLI is unavailable — for `ask_m3` that's the `mmx` CLI; fall back to your own reasoning or to `ask`. Don't block on it.
- Don't loop retrying a refusal unchanged. Change the input or move on.

## Notes

- Every call is appended to an owner-only audit log (question hashed by default). Assume it's observed; keep asks on-task.
- `ASK_FABLE_TIMEOUT` sets the per-turn timeout (default 120s). `ASK_FABLE_MINIMAX_MODEL` overrides the MiniMax model (default `MiniMax-M3`). Other `ASK_FABLE_*` knobs are owner-tuning concerns, not something you set mid-task.
- Live progress (guard result, each model asked, elapsed time, reasoning excerpts, synthesis) streams to the server's STDERR — visible in Claude Code's MCP logs / `claude --debug`. Silence with `ASK_FABLE_QUIET=1`.
- `ASK_FABLE_RETURN_THINKING=1` attaches a capped reasoning excerpt to tool results so you can read the model's inner thought process inline.
- `ASK_FABLE_STREAM_REASONING=1` streams Fable's reasoning to the stderr trace live.
- Use `reset_session(session, save=true)` to dump-and-clear an `ask` thread's transcript when you're done with a topic.
