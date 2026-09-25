# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.18.0] - 2026-09-25

### Added
- **A broken check is no longer mistaken for a failing one.** `ask_verify` classified any
  non-zero exit as the draft's claim breaking, so a snippet that died of its own
  `IndentationError` — easy, since the reviewer has to JSON-escape its Python — or of
  `FileNotFoundError` against a sandbox that deliberately has no filesystem, laundered
  into the strongest evidence class and was reported as the only objection that
  established anything. The rule is now an allowlist: a non-zero exit counts only when
  the traceback names `AssertionError`/`SystemExit` or there is no traceback at all.
  Anything else is a snippet that did not run. Sandbox output also keeps both ends when
  trimmed, because Python names the exception at the END of a traceback and a chatty
  snippet could previously push it out of view.
- **A check that ran and passed reads as `disconfirmed`**, not as "argument without
  evidence" — it is a real result, and folding it into `unbacked_objections` slandered
  the reviewer with the very figure meant to expose one that argues instead of showing.
- **`ask_verify`'s executor has a total time budget** (30s across all receipts in a call);
  twelve receipts at the per-run timeout was minutes inside one tool call. Anything unrun
  is inconclusive, never a finding.

- **`ask_verify` runs the checks it is given.** A `run` receipt may now carry real Python,
  which goes through the sandbox `ask_falsify` already uses (`ASK_FABLE_ALLOW_RUN` +
  `bwrap`, self-tested before anything is advertised). Exiting non-zero is an objection
  and reaches the strongest receipt class; exiting zero is evidence *for* the draft and
  counts for nothing, so a reviewer cannot inflate its numbers by submitting checks it
  expects to pass. A disabled, unavailable, timed-out or resource-starved sandbox is
  inconclusive and can never become a finding. Until this landed the class was unreachable
  by design, because a model writing `failed: true` was certifying its own evidence.

- **`ask_verify` — review a draft answer that already exists.** Every other mode
  reasons from scratch: a council fans a question out, a debate grows its own
  position, a falsification asserts its own claims. None of them could be handed an
  answer someone else produced, and the obvious shortcut is broken — passing the
  draft to `ask_falsify` as its `context` makes the draft its own receipt corpus, so
  every quote check passes and the answer certifies itself.

  The reviewer is asked for evidence, never for a verdict. **Code** classifies every
  objection (`verify_metrics.classify`), because a model that grades its own evidence
  is grading nothing — the rule the falsification clerk already runs on. An objection
  counts only when its receipt can be checked against something that is *not* the
  draft: a verbatim quote from the caller's `context`, or a check that ran and
  failed. Quoting the draft back at itself is classified `self-quoting` and scores
  zero, since every sentence of a draft appears in that draft.

  **It never suppresses.** The draft is returned unchanged on every path, including a
  reviewer that errors or refuses. That is the resolution of a contradiction the
  original design conference left open — one seat wanted a blocking gate, another
  warned that "when the 'defender' is also a model, is a mirror — mirrors generate
  confident nonsense". The veto moves off the answer and onto the claim, and the only
  thing that could ever suppress is code. Revision power is earned later, from
  measured precision, not argued for now: a reviewer that turns out to be decoration
  can be removed, but an answer suppressed in error is invisible to the caller.

  Reported under `verify`: `prevented` (receipt-backed objections), the separate
  `unbacked_objections` count so argument cannot pass as evidence, `self_quote_ratio`,
  and a verdict of `no-objection` / `self-quoting` / `input-grounded` / `executed`.
  No objections is explicitly **not** a correctness guarantee — it means no fault was
  demonstrated. Pass `drafted_by` to refuse a same-lab review. One model call.

- **`scripts/verify_arena.py` — a seeded-fault harness**, the only judge-free way to
  learn whether the reviewer's objections are *right*. The in-tool metrics measure
  effect and attribution; they cannot measure direction, because a confident wrong
  objection counts exactly like a true one. Feed it drafts carrying known injected
  faults and drafts known to be clean, and it reports precision on the planted faults
  beside the false-objection rate on the clean ones. Both are needed: a reviewer that
  objects to everything scores perfectly on precision alone. This replaces the
  originally pre-registered criterion of having a held-out model judge whether revised
  answers beat originals — a model grading a model certifies agreement, not
  correctness, and is gameable by fluency.

- **`ask_conference` challenges the premise nobody challenged.** After the blind
  round it names the claim every opening asserted or assumed, and assigns one
  participant — a seat that did not assert it — to make the strongest case that it
  is false, ending with a distinguishing test. That shared premise is the part no
  participant will question on its own, because agreeing on it is what lets the
  rest of the discussion happen. Two extra calls whatever the bench size; needs
  three or more seats and two or more rounds; turn it off with
  `attack_premise: false`. The chosen claim and attacker are reported under
  `premise`. (Phase 2 of `the conference design`.)

### Security

- **The `run:` sandbox caps forks again, without needing systemd.** Dropping the
  host-wide `--nproc` left nothing bounding processes when `systemd-run` is
  unavailable (no binary, or no `XDG_RUNTIME_DIR` — a headless host): `--as` is
  per-process and the pid namespace only reaps, so a fork bomb ran for the whole
  wall clock with host-wide effect. The cap now sits INSIDE the sandbox's user
  namespace, where `RLIMIT_NPROC` has been counted since Linux 5.14, so it bounds
  the sandbox and nothing else. Verified: a fork loop stops at 61 with EAGAIN.
- **A third redaction pattern could stall the server.** `_URI_USERINFO`'s scheme
  (`[a-z][a-z0-9+.-]*://`) restarted at every letter and consumed the rest of the
  run looking for `://`: 180 KB of `token-` took 80 s. Same family as the two
  above, but it predates that batch. Anchored, it takes 0.02 s.
- **A key name that CONTAINS a credential is redacted again.** The
  describes-a-credential rule (`token_type`, `api_key_id`) wrongly disarmed on
  `header`, `scope`, `status`, `source` and `field` too — so `cookie_header`, which
  holds the cookies themselves, came back in the clear.
- **The context-bus daemon validates a `Host` port instead of discarding it.**
  `[::1]evil`, `localhost:evil` and `[::1]:8788:x` all parsed as loopback. Nothing
  a browser can send from a URL, but a check that accepts malformed input fails
  open by construction.
- **A non-ASCII signature header returns 401** instead of killing the handler
  thread with a `TypeError` from `compare_digest`.
- **The Claude Code stderr echo is redacted.** Claude Code persists an MCP
  server's stderr to its own on-disk logs, so that echo is a sink like the others.

- **Redacting a tool result can no longer stall the server.** The secret-line and
  token-line patterns backtracked super-linearly on text shaped like
  `secret_secret_…` or `sk-sk-sk-…`: 56 KB took 7 s and 200 KB took 101 s,
  synchronously on the event loop, in every sink that redacts (hub turns, saved
  answers, session dumps, trace bundles). Both are bounded/anchored now and run
  linearly — 200 KB takes 0.05 s. A pasted `context` or a context-bus blob was
  enough to trigger it.
- **Plural secret keys are redacted.** `"secrets"`, `"passwords"`, `"api_tokens"`,
  `"apiKeys"`, `"private_keys"` and env-style `passphrase = …` went through
  verbatim; `ssh_key`, `jwt`, `bearer` and `pwd` are recognized too.
- **Credential metadata stays readable.** A name whose last word describes a
  credential rather than being one — `token_type`, `api_key_id`, `password_hint`,
  `credentials_file`, `tokens_used` — is no longer blanked.
- **Trace bundles use the same rule as the text redactor.** Their key filter
  disagreed in both directions: it spared every plural (`api_tokens`,
  `access_tokens`) and blanked `tokenizer`, `input_token_limit` and `secretary`.
- **A disabled `grok`/`kimi` CLI is never spawned.** A gateway token
  (`atlas:xai/grok-4.6`, `openrouter:…kimi…`) reroutes to the local CLI, and that
  path ran under the *gateway's* key, so the operator's denylist never saw the CLI
  it was about to start. Capability is not authorization.
- **The prohibited-use fold covers every combining mark and format character.** It
  listed two hand-picked ranges; 1475 more non-spacing marks and 41 more format
  characters walked a term straight through. Look-alike mappings now cover every
  letter the terms use — `b f g k m n r t v z` had none, so a single Cyrillic
  substitution was enough.

- **Redaction covers the common secret shapes it used to miss.** Quoted keys are
  now judged word by word (camelCase split), so `"access_token"`, `"refreshToken"`,
  `'client_secret'`, `"db_password"` and `"password" => …` values are redacted;
  env-style `aws_secret_access_key =`, `PASSWD=`, `private_key =` and
  `STRIPE_SECRET_KEY=` lines are caught; bare Stripe `sk_live_`/`rk_live_` (and
  `_test_`) keys are redacted. `max_tokens`/`tokenizer` keys stay readable.
- **`code_index` / `code_search` honour `pack_blocklist`.** A blocklisted file is
  no longer chunked, embedded (sent to the embed/rerank hosts) or returned; a
  stale index never serves one before the next re-index.
- **The pack blocklist is case-insensitive**, so `.ENV` / `.GIT/config` are
  refused on case-insensitive filesystems (APFS, NTFS).
- **Session transcript dumps are redacted** like every other on-disk sink.
- **`context-busd` hardening:** far-future envelope timestamps are refused (they
  could set a key's replay floor out of reach), connections time out after 30 s,
  browser requests (`Origin`) are refused and a tokenless TCP bind requires a
  loopback `Host` (DNS rebinding), and `SIGTERM` / a stale socket no longer block
  a restart — while a socket another daemon is still listening on is refused, not
  unlinked.
- **The denylist folds Unicode before matching** (zero-width and bidi characters,
  soft hyphens, fullwidth forms, combining marks, Cyrillic/Greek look-alikes) and
  matches multi-word terms across any whitespace, `_` or `-`.

### Changed

- **The council synthesizer now sees the code.** It was given the question and the
  expert answers and nothing else, so the model told to resolve disagreement "on
  the merits" about a piece of code could not see that code, and had only the
  experts' rhetoric to judge by. It now receives the same context the panel did,
  clamped with a marker naming what was dropped. The synthesis prompt gained the
  data-not-instructions clause it never had — it rides the system channel instead
  of the panelist scope prompt, so it needs its own. The answer cache version is
  bumped, since cached councils were synthesized blind.
- **The conference's blind round runs concurrently.** Round 1 is blind by
  construction, so serializing it only cost wall-clock: a default five-model,
  three-round run was sixteen calls in series. Answers are still committed in
  bench order, not completion order. Rounds 2 onward stay sequential.
- **The conference rapporteur no longer judges an argument it took part in.** The
  default synthesizer sits on the default bench, so it was mapping a discussion it
  had argued in, with everyone named. It now reads an anonymized transcript with
  its own seat last, as the council's synthesizer already did, and the new
  `map_legend` maps the pseudonyms back.
- **The conference speaking order rotates each round**, so the first seat no longer
  always argues without peer context while the last always has the most.
- **The conference transcript is budgeted.** It was rebuilt in full for every turn
  with no cap. The seed and the whole blind round are always kept — the blind round
  is the only independent evidence in the run — and the middle is elided with a
  marker.
- **The last round asks participants to land a position** and name the one fact that
  would change their mind, instead of repeating the same open-round prompt.

- **An `http`-transport follow-up is refused instead of answered memoryless.** An
  http turn now returns an `http:` marker session id, so a follow-up on the same
  session gets `transport_incapable` (reset the session to start over), as
  `docs/CONFIGURATION.md` already promised. Only hosts that pin
  `ASK_FABLE_FABLE_TRANSPORT=http` are affected — `http` is never on the `auto` ladder.
- **An explicit `models` list that names nothing recognized is `bad_args`.**
  `ask_council` used to run the default panel instead; `ask_conference` now
  answers `no_models` with the `unknown` tokens.
- **Model-written `metamorph` receipts no longer count in `ask_falsify`.** Untagged
  metamorph receipts in existing ledgers stop supporting their claims (`metamorph=true`
  re-checks them).

### Fixed
- **`ask_fable_help("tools")` lists `ask_falsify` and `ask_conference`.** Neither was
  ever added when it shipped, so the in-server manual described a server two tools out
  of date.

- **LM Studio never unloads a model that is still generating.** The in-flight count
  was released when the awaiting coroutine unwound, but the POST runs in a thread
  `asyncio.to_thread` cannot cancel — so a council that hit its timeout dropped the
  count mid-generation and the cleanup unload right after it pulled the model out
  from under a live request, the one thing `unload` promises never happens. A
  second counter, raised and lowered by the thread itself, now outlives the
  cancellation. The engine-crash memory probe claims the model before its own
  unload for the same reason.
- **A cut-off answer never RAISES a council's confidence.** The penalty was written
  as a swap rather than a ladder, so an already-low confidence was promoted to
  medium — inverting the signal in the two worst cases (a lone panelist whose answer
  was cut off, and a failed synthesis plus a cut-off panelist).
- **A council that materially disagreed still says to escalate**, even when one of
  its answers was also cut off; the cut-off notice no longer displaces that advice.
- **`ask_conference` counts seats the way `ask_council` does.** A model the operator
  disabled is a deliberate choice, not a missing answer, so it is reported under
  `disabled` and left out of the quorum denominator; `detail` uses that denominator
  too rather than contradicting `quorum`.
- **The council cache key covers the synthesizer's default effort**, so changing
  `ASK_FABLE_CODEX_REASONING` no longer serves a stale codex-synthesized verdict.
- **Whether a model could be freed is no longer frozen into a cached answer.**
- **A council says when it could not put the box back.** `unload` reports a refusal
  or a failure in its return value rather than raising, and the sequential LM Studio
  path discarded it, leaving a model resident while the docstring promised
  otherwise. Failures now surface under `cleanup_failed`.
- **An operator `kimi` config with a bare `thinking` key no longer breaks every
  call.** `thinking = true` (or an inline `thinking = { … }`) collided with the
  `[thinking]` table ask_fable writes, making the merged file a "Cannot overwrite a
  value" parse error — the same failure the `[thinking]  # tuned` fix closed for the
  table spelling.
- **`room_verdict` returns `unknown` for a size it cannot use.** A negative size
  floor-divided to `-1` and read as `fits`; a numeric string from the control page
  reached the comparison and raised.
- **A second `SIGTERM` no longer skips the context-bus daemon's cleanup**, and a
  child killed because its caller had already been cancelled is reaped rather than
  left a zombie until CPython's next sweep.
- **The `ubercode` and `uberplan` skills document the real council contract** —
  `consensus` is a string (`strong`/`partial`/`divergent`/`unknown`), so `if
  consensus:` is true even for `divergent` — and the error kinds that name what to
  change rather than something to retry.

- **An answer the provider cut off no longer passes as a complete one.** A
  `max_tokens` stop returns `status: "ok"` with `kind: "truncated"`, and the
  council, chain, debate, conference and `ask` all filtered on status alone: the
  half answer counted toward quorum, was synthesized as if whole, and the merged
  verdict was frozen in the cache for the full TTL. They now name the cut-off
  models under `partial`, read as `degraded`, say so in
  `recommended_next_action`, and refuse to cache. `ask` carries the bridge's
  `partial` / `stop_reason` the way `ask_model` already did.
- **A missing CLI no longer takes a backend offline.** `binary_missing`,
  `sdk_unavailable`, `bad_args`, `model_not_found`, `context_too_small`,
  `transport_incapable`, `disabled` and `busy` are permanent config/request
  states, so five in a row replaced the actionable message ("install codex") with
  `circuit_open` for the cooldown — and with a pinned SDK transport and no Claude
  Code binary, it tripped the breaker for **fable**.
- **The tool cache keys on the effort a call actually runs at.** `effort=None`
  means "the operator's default", which lives in env/config and outlives the
  process — as `cache.db` does. The oracle layer resolved it, but the outer hit
  returned first, so `ask_model(provider="grok")`, `ask_council`, `ask_chain` and
  `ask_debate` kept serving an answer computed at the old default. `codex`'s
  reasoning effort is keyed at all now.
- **A non-UTF-8 `claude --version` no longer escapes `fable.run`.** It raised out
  of `best_cli_path()`, which runs before the bridge's error handling, so the call
  left no audit row, no breaker record, and `auto` never fell through to the CLI.
  (`diagnose` fixed its own copy of this probe; this is the sibling it left.)
- **A timed-out `ask_debate` reports the disagreement it never settled.** The
  clock running out left `contested_claims_remaining: 0` and
  `material_disagreement: false` — the field the skills tell agents to key on.
- **`diagnose` reports a disabled backend as disabled**, instead of "`mmx` not on
  PATH — install the CLI" for a CLI that is installed, or a cheerful `ok` for a
  disabled `fable`. A binary that is present but unusable (kimi without its
  `config.toml`) says so rather than claiming it is missing.
- **`stats` no longer counts a council member that never ran.** A member cancelled
  while still queued records a 0 ms span; counting it inflated `calls` and dragged
  `avg_ms` toward zero on every `tier="full"` run.
- **`ask_conference` counts the seats it could not fill.** Unknown and unavailable
  models were left out of the quorum denominator, so `["fable","opus","gpt-5"]`
  read a full `2/2` where the council called the same list `2/3, degraded`.

- **`ask_council` no longer answers with models nobody named.** A partly-unknown
  `models` list reports the tokens under `unknown` and counts them as requested, so
  `["fable", "gpt-5", "deepseek"]` reads quorum `2/3`, `degraded: true` rather than a
  `strong`, "safe to act" `2/2`; `ask_chain` does the same (`gpt5 > glm > fable`
  reports `requested: 3`). Unknown tokens are part of the cache key.
- **Disabled backends stay off in every council path.** A backend turned off with
  `configure_disabled` / `ASK_FABLE_DISABLED` is dropped from the panel before quorum
  is counted and reported under `disabled`. It no longer synthesizes (with no enabled
  synthesizer left the council returns its first answer, flagged unsynthesized), no
  longer rescues a failed `ask_chain`, and `ask_websearch` refuses it with
  `kind: "disabled"`.
- **A council whose synthesis failed no longer says "safe to act".** Its
  `recommended_next_action` now says the answer is one panelist's raw reply.
- **A bare synthesizer id on the OpenRouter council is an OpenRouter id.**
  `synthesizer="openai/gpt-5.6-sol"` with `provider="openrouter"` was turned into an
  Atlas token. A value already saved as `atlas:…` keeps it; re-run `configure_council`.
- **`ask_council(provider="ollama")` honors `synthesizer`**, and an invalid one is
  `bad_args`.
- **The LM Studio council no longer unloads a model that was already resident.**
  Members and a local synthesizer are matched against the pre-run residents the way
  `run`/`unload` match them (case-insensitive, across `@variant` suffixes).
- **`ASK_FABLE_COUNCIL_TIMEOUT` / `ASK_FABLE_CHAIN_TIMEOUT` are documented as what
  they bound** — the panel and the stage pipeline; synthesis and the chain's fallback
  run after them.
- **A timed-out `ask_debate` no longer returns the opponent's attack as its verdict.**
  It falls back to the proposer's latest position, and `degraded_timeout` downgrades
  its self-declared confidence like a stalemate does.
- **`ask_conference` reports who never spoke, and why.** Explicit `models` get the
  default bench's availability check (`unavailable`), failed turns appear under
  `errors`, a failed rapporteur under `map_error`, and fewer than two voices is
  reported through `quorum` / `degraded` / `detail`. `session` now keys the hub
  mirror, and the call is audited and saved like the other `ask_*` tools.
- **Token counts are no longer blanked in tool results.** Any key containing
  `token` was redacted, so `stats` `total_tokens`, `effort_choices[].max_tokens` and
  `trace_get` usage read `"[REDACTED]"`; count keys are left alone while `token`,
  `access_token`, `id_token` and other credential keys stay redacted.
- **`list_models(provider="openrouter", task=…)` no longer crashes with form
  elicitation** (KeyError `picker_description`); it uses OpenRouter's wording and
  effort default and returns the plain listing if the picker fails.
  `list_models(provider="atlas")` no longer blocks the event loop.
- **`context(op="pack")` no longer silently drops a line range listed alongside its
  whole file** when the whole file is refused; the range is tried in its place.
- **`configure_disabled(enable=[x])` re-enables the last entry of an env-set
  denylist** (stored as an explicit empty override; `set: []` still returns to the
  env list).
- **`reset_session` accepts every Opus spelling `ask(oracle=…)` does**, and rejects a
  Fable session label starting with `opus5:`, which aliased the Opus session.
- **An empty `ASK_FABLE_SAVE` no longer turns answer saving on.**
- **LM Studio no longer freezes the server while a model loads.** The in-flight
  refcount has its own lock instead of sharing the load lock on the event loop, and
  an unload claims its model so a chat can't start on one mid-unload.
- **`lmstudio_swap=auto` puts back what a failed swap unloaded**, no longer crashes
  (`not enough values to unpack`) and no longer reports a successful restore as
  failed.
- **`ask` follow-ups resume on the `claude` CLI transport** (`--resume`); they ran
  fresh with no memory while reporting ok.
- **Claude Agent SDK failures no longer escape `fable.run`.** A missing Claude Code
  binary is `binary_missing` (so `auto` falls through to the CLI), other SDK errors
  are classified results, and a resume id whose conversation is gone (read from the
  process's captured stderr) is dropped so the session recovers — a transient
  failure (429, overload, startup timeout) keeps the id.
- **A NUL byte in a prompt no longer crashes the CLI bridges** — it is reported as
  `bad_input`, which doesn't count against the circuit breaker — and a lone
  surrogate no longer crashes the answer cache key or the trace's argument hash.
  (Over the stdio transport a lone surrogate is rejected by the JSON parser before
  it reaches a handler, so this is defence in depth, not a reachable crash.)
- **The session hub keeps projects apart when it sweeps or prunes.**
- **`ask_falsify`'s clerk closes its loopholes.** A model-supplied `metamorph`
  receipt is dropped and clerk-only fields are stripped at ingest; restating a live
  claim no longer changes its status and only this turn's receipts are verified;
  attacks on an already-killed claim are ignored; a contra must register a genuinely
  new claim; a `fixpoint` status resets each round.
- **The calibration store records outcome flips** instead of keeping a claim's first
  result forever.
- **A ``` inside a JSON string no longer truncates a sidecar/debate/falsify block**
  — the closing fence must start its own line.
- **`trace_list`/`trace_get` see recent traces on a large audit log** (newest-first
  scan), and provider spans record their real start time.
- **`stats` reports a true nearest-rank p95 and counts incomplete calls** (client
  aborts under `cancelled`, crashes under `errors`).
- **`diagnose` survives a failing probe** and non-UTF-8 `--version` output.
- **HTTP bridges no longer raise when a connection drops mid-response.**
  `RemoteDisconnected`, `IncompleteRead`, a connection reset or a TLS EOF escaped
  the Anthropic-compatible, Atlas, OpenRouter and Ollama bridges and their catalog
  lookups, aborting a whole `ask_chain` (discarding answered stages) and skipping
  the council's fallbacks. They now return a network error with the same single
  retry as a 5xx; the Ali catalog, the embed host and the control page read treat a
  cut-off body as unreachable too.
- **A provider refusal over the Anthropic HTTP transport is reported as a
  refusal.** `stop_reason: "refusal"` used to become `sdk_error` (feeding the
  breaker for the default synthesizer) or a cached partial answer; it is now
  `refused` / `provider_refusal` and the partial text is discarded.
- **Answers cut off at the output cap are flagged and never cached.** A
  length / `max_tokens` stop keeps the partial text as `kind="truncated"` with
  `meta.partial`; a reasoning model that spends the whole budget thinking gets the
  new non-health `budget_exhausted` kind instead of opening the breaker. This covers
  every gateway, LM Studio, and the Claude SDK / CLI transports.
- **The oracle answer cache keys on the oracle, not just the model id.**
  `atlas:<id>` and `openrouter:<id>` no longer share answers, a mid-flight 5.1 → 5
  demotion is never cached under the pinned `fable51`, and a default effort is keyed
  on what it resolves to. The cache version is bumped, so old entries are dropped.
- **OpenRouter no longer downloads its catalog on the event loop** for the
  reasoning-effort lookup, and it remembers misses and failures.
- **Codex, Gemini and Grok refuse over-large prompts instead of tripping the
  circuit breaker.** A prompt past the ~128 KiB argv limit is `context_too_large`
  (non-health) up front instead of five failed spawns taking the backend offline.
- **Large prompts for gateway Grok and Kimi models reach the gateway.** The
  size-capped local CLI is used only when the prompt fits and it can run — for
  council/chain tokens and single-model `ask_model(provider="atlas"|"openrouter")`
  alike; the old "use the gateway" advice routed straight back to the same CLI.
- **Kimi's size check runs before its sandbox is built**, `kimi` is no longer
  reported available without the `config.toml` its sandbox needs, and an existing
  `[thinking]` table written as `[thinking]  # tuned`, `[ thinking ]` or
  `["thinking"]` no longer makes every Kimi call fail to parse its config.
- **OpenRouter moderation refusals and out-of-credit errors are classified
  correctly.** A flagged-input 403 is a provider refusal rather than `auth_failed`,
  and a 402 is the new non-health `payment_required` kind.
- **`openrouter:x-ai/grok-*` tokens run on the local `grok` CLI with the right
  model name** (`x-ai/` is stripped like `xai/`).
- **One failed context-bus request no longer leaves the store "degraded" until
  restart** — every store operation clears the last error first.
- **Listing the context bus works however much it holds.** With `{"bounded": true}`
  the daemon returns every row's metadata and an envelope only while it is ≤ 256 KiB
  within a 16 MiB total (`description_omitted` otherwise); local listings no longer
  load every value.
- **`path:START-END` packs read in bounded pieces** (a 400 MB one-line file peaked at
  813 MiB), and whole-file reads stop at the cap.
- **`code_search` line ranges match `context_pack` again** (`\n`-only line
  numbering; affected files are re-chunked once), and a malformed embed reply falls
  back to keyword search instead of crashing.
- **`run:` receipts work on busy desktops.** The sandbox no longer sets the host-wide
  `--nproc` limit; a sandbox setup error, EAGAIN or failed spawn reports
  `unavailable` (inconclusive), and an empty snippet is an input error even without
  bwrap.
- **Storage no longer chmods existing directories to 0700** (a shared `1777` dir, or
  `/run` as root); only directories ask_fable creates — missing parents and the
  oracle cwd included — are set to 0700.

## [0.17.0] - 2026-09-23

### Changed
- **Consolidated the council, configure, and context surfaces (advertised count
  37 → 27).** `ask_opus5` folds into `ask(oracle="opus")` (the whole Opus family
  names the one Opus session, mirroring `reset_session`); the four provider
  councils fold into `ask_council(provider="ollama"|"atlas"|"openrouter"|"lmstudio")`;
  the three council-config writers fold into `configure_council(provider=…)`; and
  the five context tools fold into `context_read(key?)` (read one blob, or list
  when the key is omitted — stays read-only) plus `context(op="write"|"pack"|
  "delete", …)` (the mutating half, honestly flagged destructive). Every
  consolidated call routes to the SAME per-provider handler it did before, so
  internal titles/cache keys/hub-session labels and the sequential LM Studio path
  are preserved. All folded names (`ask_opus5`, `ask_*_council`,
  `configure_*_council`, `context_write/pack/list/delete`) remain **callable as
  unadvertised aliases**. `ask_tracing`/`configure_disabled`, `code_index`/
  `code_search`, and the chain/debate/falsify/conference modes are unchanged.
  `lhm.plugin.json` needs regenerating at release.
- **Consolidated the single-model tool surface into `ask_model(provider, model?)`
  and the catalogues into `list_models(provider)`.** The 13 per-backend
  single-model tools (`ask_sonnet`, `ask_m3`, `ask_glm`, `ask_deepseek`,
  `ask_gemini`, `ask_codex`, `ask_grok`, `ask_kimi`, `ask_ollama`, `ask_lms`,
  `ask_atlas`, `ask_ali`, `ask_openrouter`) and the 5 catalogues
  (`list_ali_models`, `list_atlas_models`, `list_openrouter_models`,
  `list_ollama_models`, `list_lms_models`) are no longer advertised — they remain
  **callable as unadvertised aliases**, so an existing client, skill, or bookmark
  keeps working. `ask_model` takes `provider` (the backend) plus an optional
  `model` (a CLI override or a gateway model) and `effort`; a `model` passed to a
  fixed-model provider (`minimax`/`glm`/`deepseek`/`sonnet`/`gemini`/`codex`) is a
  `bad_args` error rather than a silent no-op. The multi-turn `ask`/`ask_opus5` and
  the opt-in `ask_websearch` are unchanged. Internally every call still routes
  through the same per-provider handler, so audit `tool` labels, hub `session`
  labels, and cache keys are preserved and `stats(by="tool")` continuity is intact.
  The advertised surface drops from 53 tools to 37; `ask_fable_help("tools")` and
  the standing instructions point at the new tools. `lhm.plugin.json` needs
  regenerating at release.

### Added
- **Alibaba Cloud (Qwen) reasoning models via the `ask_ali` tool and `ali:<model>`
  council tokens.** The token-plan MaaS gateway speaks the Anthropic Messages API
  (`/apps/anthropic/v1/messages`) and returns real `thinking` blocks, so a turn
  reuses the existing `anthropic_http` client — the same one glm/deepseek ride —
  with the selected model per call. `list_ali_models` lists the live catalog
  (fetched from the gateway's OpenAI-compatible endpoint, since the Anthropic app
  exposes none), filtered to the **reasoning LLMs** (Qwen `qwen3.x-*`, plus the
  deepseek-*/glm-*/`auto` it fronts) — the audio/TTS/image models are dropped
  unless `all=true`. Models are usable anywhere a gateway token is (`ask_council`
  /`ask_chain`/`ask_debate`) and honor the denylist (`ali` disables the whole
  provider). Auth: `ASK_FABLE_ALI_API_KEY` (a token-plan key for this gateway;
  `ASK_FABLE_ALI_BASE_URL` overrides the host); billed per token, so results carry
  `cost_basis="billed"`.
- **`opus` is now a model LADDER that tracks the newest Claude Opus, like
  `fable`.** It walks `OPUS_CANDIDATES` newest-first (`claude-opus-5-5`, then
  `claude-opus-5`) and asks for the best id the local Claude Code build hasn't
  rejected, so `ask_opus5`, the `opus` token, and the `twin` group all follow the
  newest Opus automatically. A build too old for 5.5 demotes it once and answers as
  Opus 5 instead of failing. `ASK_FABLE_OPUS_MODEL` pins an exact id. New pin
  tokens name a specific version and never ladder: **`opus55`** (Opus 5.5),
  **`opus5`** (Opus 5), and the existing **`opus48`** — with `opus-5` /
  `claude-opus-5` now resolving to the `opus5` pin (not the ladder) and
  `opus5.5` / `opus-5.5` / `claude-opus-5-5` to `opus55`. Same-lab pins, so they're
  excluded from the council tiers exactly as `fable51`/`opus48` are. The
  version-neutral machinery lives in `fable.Ladder`; `ask_opus5` keeps its name for
  back-compat while now meaning "newest Opus".
- **Turn oracles/providers OFF at runtime: the `configure_disabled` tool and the
  `ASK_FABLE_DISABLED` denylist.** Name an oracle key or alias (`grok`, `m3`,
  `opus48`) or a whole provider (`atlas`, `openrouter`, `ollama`, `lmstudio`); a
  disabled backend is dropped from every council/tier, refused by its dedicated tool
  with `kind="disabled"` (distinct from `not_configured`), and can't be reached
  indirectly (a disabled Atlas no longer serves the `glm` fallback). Config wins over
  the env var, so the tool toggles it with no restart; call it with no args to see
  the current list.

## [0.16.1] - 2026-09-21

### Fixed
- **Oracle subprocesses no longer inherit the caller's working directory, so no
  external CLI can read the surrounding repo.** Every CLI bridge (`claude`, `codex`,
  `agy`/Gemini, `grok`, `kimi`, `mmx`) spawned in the calling agent's cwd, so a tool
  that discovers instructions by walking *up* from cwd — Claude Code's
  `CLAUDE.md`, Codex's/agy's `AGENTS.md`, project resolution — could pull the
  private repo's files into a prompt the caller never sent. Bridges now spawn in a
  controlled, empty, ask_fable-owned dir (`isolation.oracle_cwd()`, under the XDG
  state dir) that has nothing for discovery to find. The Claude bridge adds three
  more layers: `--safe-mode` (no `CLAUDE.md`/`AGENTS.md`/skills/plugins/hooks/MCP,
  while keeping the flat-plan OAuth session), env opt-outs for auto-memory and the
  bundled/policy skill listings, and `tools=[]` on the SDK path — which previously
  shipped the full built-in tool schema (~109 KB) in the system prompt even though
  `allowed_tools=[]` blocked their use (the CLI path already passed `--tools ""`).
  The opt-in `ask_websearch` path still gets exactly `WebSearch`/`WebFetch`.

## [0.16.0] - 2026-09-20

### Added
- **Configurable Anthropic transport: `ASK_FABLE_FABLE_TRANSPORT=auto|sdk|cli|http`.**
  The whole Claude family (`fable`, `fable51`, `opus`, `sonnet`, `opus48`) was
  reachable only through Claude Code — the Agent SDK or the `claude` CLI, both riding
  the OAuth session — so a host with neither failed `binary_missing` even with a valid
  `ANTHROPIC_API_KEY`. The new `http` transport reaches the Messages API directly with
  `ASK_FABLE_ANTHROPIC_API_KEY` (base URL overridable), reusing the existing
  `anthropic_http` client and keeping the same oracle key and lab, so council seating
  and attribution are unchanged. Design rules that carry it: **`auto` never picks
  `http`** (an exported key must not silently convert a flat-plan oracle into a
  per-token one); a **pinned transport never falls back**; only "this transport is not
  here" (`binary_missing`, `sdk_unavailable`) may re-route — a refusal, timeout, 429 or
  rejected model propagates untouched; and the plan is resolved **once per call**, not
  per dispatch (the model ladder dispatches twice, so resolving lower could answer one
  question over two transports). The `http` transport reports `cost_basis="billed"`,
  and refuses a `resume=` turn as `transport_incapable` rather than answering from a
  different oracle. `ASK_FABLE_USE_CLI` is now an alias. The model ladder's
  demotion set is keyed `(transport, model)`, so a Messages-API rejection no longer
  poisons that id on the OAuth path.
- **`ask_websearch` works over the `http` transport**, via the API's own
  **server-side** `web_search` tool — so `ask_websearch(model="fable")` now runs on a
  host with no Claude Code at all (its earlier blanket `transport_incapable` refusal
  is gone; only `resume=` remains impossible there). Deliberately the **basic** tool
  version: `web_search_20260209` and later default `allowed_callers` to code execution
  and return a 400 on a model without programmatic tool calling unless
  `allowed_callers: ["direct"]` is also sent. The API reports a *failed* search inside
  an HTTP 200 (a `web_search_tool_result` whose `content` is an error object rather
  than a list), so a turn whose searches all failed returns an error instead of a
  "researched" answer with no research behind it (`too_many_requests` classifying as
  `rate_limit`); a partial failure still answers, with the codes on `search_errors`.
  A long turn that comes back `pause_turn` is resumed (capped at 4 continuations),
  accumulating content blocks and usage across the resume. The result carries
  `sources` (the URLs the answer cites), the billed `web_search_requests` count, and
  one `tool_event` per search. Budget reuses `ASK_FABLE_WEBSEARCH_MAX_TURNS` as
  `max_uses` — ≤ $0.20 a call at the default, since searches bill at $10/1,000.
- **`diagnose` no longer reports Anthropic oracles as reachable on a host with no
  Claude Code.** It checked the session's existence, not Claude Code's, so a box with
  neither the SDK nor the `claude` CLI printed `ok` with no `fix:` while every call
  failed. It now probes presence (SDK importable / `claude` on PATH — no network, no
  token refresh) and accepts the `http` transport when it is pinned and keyed, with a
  `fix:` line naming both remedies.
- **`ask_websearch` gains a `gemini` backend** (`model="gemini"`, aliases `agy`,
  `gemini-3.1-pro`): live web search through the local `agy` CLI, on the same
  flat-plan Google login `ask_gemini` already uses. It is **search-only by
  design.** Probed on the installed build, `agy` in print mode runs under its own
  headless permission policy — `search_web` is allowed, while page fetch
  (`read_url`), shell commands, and file/dir tools are auto-denied, and a denial
  does not degrade: the turn aborts and prints *nothing* on stdout with rc 0, so a
  single fetch attempt costs the whole answer. This backend therefore gets the
  research prompt plus an explicit "`search_web` ONLY" constraint, with the base
  prompt's "you have page fetch" clause rewritten out (anchor asserted, so
  rewording the base prompt fails loudly instead of re-arming it). Use `grok` or a
  Claude model when you need page fetch. `agy` defines no `--allowed-tools` /
  `--disallowed-tools` / `--permission-mode` flag at all, and `--sandbox` restricts
  only the terminal — so there is no way for us to gate it ourselves; the policy
  above is agy's own. Companion fix: an empty `agy` answer now carries the
  auto-denial text from stderr back to the caller, instead of a bare "Gemini
  returned no answer" — that message names the denied tool *and* the fix.
- **`ask_websearch` — an opt-in web-search / OSINT research agent (the ONE tool
  that browses).** Every other `ask_*` oracle is deliberately toolless; this one
  runs a search-capable model with live web search ON and returns a sourced,
  cited answer. Pick the backend with `model`: `grok` (grok-4.6 live search, the
  default) or a Claude model on the OAuth session — `sonnet` / `opus48` / `opus5`
  / `fable` — using native `WebSearch`/`WebFetch`. All are flat-plan sources, so a
  search costs nothing beyond the subscription. **Off by default:** set
  `ASK_FABLE_ALLOW_WEBSEARCH=1` (env or config) to enable; otherwise the tool
  returns `{"status":"disabled",...}`. Results are never cached (web facts are
  time-sensitive). Only `WebSearch`/`WebFetch` are re-enabled — Bash/Read/Write and
  the rest stay blocked on both transports. New knobs:
  `ASK_FABLE_WEBSEARCH_MODEL` (default `grok`) and `ASK_FABLE_WEBSEARCH_MAX_TURNS`
  (default 20).

### Fixed
- **The guard now scans `context`, not just the question (Layer 2b).** The model
  provider's own safeguard reads the WHOLE payload, so framing parked in `context`
  could pass the question-only check and then be refused upstream — opaquely, with
  no audit row and (on the Anthropic path) an inflated error rate. The denylist now
  runs over `context` too, so that block is deterministic and local. On by default;
  `ASK_FABLE_GUARD_SCAN_CONTEXT=0` restricts the scan to the question, and
  `trusted=true` lifts it as before. The refusal payload carries `where`
  (`question` / `context`) so a caller strips the framing from the right field, and
  `trusted` is now advertised on every question-taking tool (previously `ask` only).
- **A provider-safeguard refusal is now classified as `refused`, not `sdk_error`.**
  The Claude SDK reports a safeguard block as an error (`stop_reason="refusal"`),
  so it was landing in `stats` as an error, counting toward the circuit breaker
  (refusals don't trip it; errors do), and reaching the caller as an opaque error.
  It now maps to our `refused` status with `kind="provider_refusal"` on both the
  SDK and CLI transports, and carries a provider-specific `how_to_reframe` (try
  another backend; never resend unchanged).
- **A turn-budget wall no longer discards the research.** When the `ask_websearch`
  agentic loop hit `error_max_turns` mid-search, the whole turn (75–120s of work)
  was thrown away for a bare error. Any prose already emitted is now returned as a
  `kind="truncated"` partial (never cached), marked `partial: true`; an empty turn
  is still an error. The web-search system prompt also gained a budget-discipline
  instruction (corroborate, stop, reserve the last turn to write the answer).
- **`stats` no longer hides guard refusals in its default view.** A guard denial
  carries no model, so `by="model"` could not bucket it — and the record was
  dropped entirely, *including from totals*, so a window of guard refusals read as
  `refused: 0` (contradicting the documented "a denial IS a refusal"). Unbucketed
  records now reach `totals` only, the same treatment `circuit_open` already got:
  no phantom `"?"` bucket in the breakdown, but the denial still counts.

- **Context-bus writes are one atomic guarded upsert now (no lost update).** The
  daemon's PUT did a SELECT then a separate upsert; because it is a threaded server, two
  writers could read the same stored `writer_ts`, both pass the floor check, and the later
  committer win with the OLDER timestamp — a lost update / rollback. The read-and-write is
  now a single `INSERT … ON CONFLICT … WHERE` that SQLite serializes under the write lock,
  with the `If-Match` compare-and-set folded INTO the WHERE (checking it beforehand let two
  racers both pass on the same stale read). The 60s skew slack is deliberately kept — it is
  the same window `context_hwm` enforces on the read side — so an in-slack older write still
  lands; only a genuinely stale (beyond-slack) or If-Match-mismatched write is refused, now
  with a 409 carrying the current ts so the client can re-seal.
- **Hub `session_meta` no longer collides across projects on one machine.** Its primary key
  omitted `project`, so the same `(session_key, agent_id)` — e.g. the default session under
  the default agent — run in two repos shared one row: each write flipped the row's `project`
  and blanked the other project's `session_list`. The key is now
  `(project, session_key, agent_id)`. A bare `CREATE TABLE IF NOT EXISTS` is a silent no-op
  on an existing DB (after which the new `ON CONFLICT` would raise and be swallowed, stopping
  the mirror), so a table with the old key is detected by its stored DDL and rebuilt. The
  mirror is a rebuildable cache — `session_peek` reads the untouched append-only `turns`
  table — and old rows already merged two projects' counts under the colliding key, so it
  drops and recreates empty rather than copying wrong data.
- **Code index no longer follows symlinks out of the tree.** `_iter_files` walked with
  `os.walk(followlinks=False)` (so symlinked *directories* were skipped) but still `stat`'d
  and read symlinked *files*, so a symlink inside the root pointing outside it (e.g.
  `notes.md -> ~/.ssh/id_rsa`) was read and indexed — its contents landing in the local
  index and, if embed hosts are configured, POSTed to them. Symlinked files are now refused;
  a real file inside the root is still indexed on its own path.
- **Code index degrades to keyword search instead of crashing on an embed vector-count
  mismatch.** If an embedding host returned `ok` with fewer vectors than chunks, the strict
  `zip` raised an uncaught `ValueError` (only `sqlite3.Error` was caught) and aborted the
  whole index call. The mismatch is now detected and turned into the existing keyword
  fallback.
- **LM Studio's pre-load context check reserves the same margin as the run check.** The
  pre-load guard tested `est + min_output > max_ctx`, but the run guard subtracts an extra
  `_CONTEXT_MARGIN`, so a prompt in that ~256-token band passed pre-load, loaded the model
  into VRAM, then failed immediately with `context_too_small` — a wasted load. Both pre-load
  checks now use `_needed_context(est)` (prompt + min output + margin), consistent with the
  ceiling and loaded-window checks.
- **LM Studio: a chat re-verifies the resident model right before sending.** Between
  choosing the model/context window and sending the request, the model could be unloaded —
  by a same-process unload that lost the in-flight race, or (deterministically) by a *second*
  ask_fable process, whose unload can't see this process's in-flight refcount. Sending anyway
  made LM Studio JIT-reload the model at its app-default (~4k) context and silently truncate
  the prompt — the worst failure class, because the answer still comes back confidently. The
  bridge now re-reads the catalog immediately before sending and fails loudly
  (`model_unavailable` if it was unloaded, `context_too_small` if it came back with a smaller
  window) instead of truncating; a re-ask reloads it at the needed window.
- **`reset_session` no longer races an in-flight `ask` on the same session.** The reset
  handler was synchronous and skipped the per-session lock, so a reset could land between an
  in-flight ask's model await and its `record_turn`, which then RESURRECTED the just-cleared
  session with the finished turn — and the caller who reset inherited stale conversation
  state. Reset is now async and takes the same `_session_lock` the ask pipeline holds, and
  both derive the namespaced key through one `_session_key()` helper so they can't drift onto
  different locks (which would make the guard a silent no-op). Reset waits for the in-flight
  turn to be recorded, then clears it — so a `save=True` dump captures that final turn rather
  than dropping it.
- **`code_index` / `code_search` / `trace_list` / `trace_get` no longer block the event
  loop.** They ran their `os.walk`, file hashing, synchronous HTTP embed calls, and
  multi-MB audit-log scans directly on the single asyncio loop, freezing every other
  in-flight MCP request for the duration. They now run on a worker thread via
  `asyncio.to_thread`, matching what `stats` already did.
- **Six tools are now input-validated server-side.** `ask_lms`, `ask_lms_council`,
  `list_lms_models`, `unload_lms_model`, `host_status`, and `diagnose` were registered in
  `list_tools` but missing from `_TOOL_SCHEMAS`, so `_schema_error` returned `None` (no
  validation) and e.g. `ask_lms` ran on an empty question instead of failing fast. They're
  wired to their existing schema objects.
- **A degraded orchestration result is no longer cached.** A degraded council (lone-survivor
  1-of-N, or fully-failed synthesis), a chain that fell back to an earlier stage, and a debate
  whose adjudicator was unavailable were all cached with `status: ok`, so a transient blip
  that felled the run froze the degraded answer for the whole TTL and re-asking (the natural
  remedy) was a no-op for an hour. None of those degraded paths cache now; the healthy paths
  (synthesized council, completed chain, adjudicated/stalemate debate) still do.
- **Council fan-out no longer leaks model tasks on client disconnect.** `asyncio.wait` does
  not cancel its input tasks when it is itself cancelled, so an outer cancellation skipped
  the pending-cancel loop and orphaned the model tasks — burning quota and holding
  parallelism slots. The wait is wrapped so an outer cancellation cancels every task on the
  way out.
- **`ask_council` / `ask_conference` schemas accept `lmstudio:` tokens.** The `models` item
  schema listed `ollama:` / `atlas:` / `openrouter:` patterns but omitted `lmstudio:`, so
  strictly-validating MCP hosts rejected documented LM Studio council/conference calls.
- **`ask_falsify` detects a concurrent ledger overwrite on the local store.** The ledger is a
  whole-blob read-modify-write, so on the local SQLite backend two processes on one machine
  could each save and silently clobber the other's claims. `context_store` rows now carry a
  `version`; `get_versioned` reads it and `put(…, expected_version=…)` writes only if the
  version still matches (optimistic concurrency), so a losing `ask_falsify` save returns False
  and the run reports `ledger_persisted: false` instead of believing it saved. Scope: the local
  backend only — the LAN context bus keeps its own newest-wins control (a version precondition
  there is a daemon-protocol change, deferred). No blob merge: claim ids are model-assigned and
  not unique across concurrent runs, so a merge-by-id would be unsafe; the losing run re-runs.
- **`ask_falsify` reputation calibration no longer double-counts, and same-session runs are
  serialized.** Two concurrent `ask_falsify` calls on one session both loaded the same ledger
  and fed the same resolved outcomes into the reputation store twice — idempotency lived only
  in a `_calibrated` flag written back into the ledger *blob*, which fails if the blob save
  fails or if a sibling loaded before it. Reputation is now recorded under a durable
  `(session, claim)` idempotency key in the DB (`outcomes` table, `ON CONFLICT DO NOTHING`), so
  advancing or replaying a ledger records each outcome exactly once regardless of blob saves;
  the claim key binds the model-assigned id to a hash of the claim text so two runs' colliding
  ids don't merge. `score`/`snapshot` derive from that single table (the `_calibrated` mark is
  gone, and with it the extra whole-blob save that was the likeliest to clobber a sibling). A
  per-session lock now serializes overlapping runs on one session, failing fast with
  `session_busy` rather than clobbering a sibling's ledger or blocking a caller for minutes.
  (Per-process; the cross-process ledger compare-and-swap is a follow-up.) One-time note: this
  resets the aggregate calibration store — old `reputation`-table rows are abandoned; the store
  is data-starved and not yet read in production.
- **`ask_falsify` lab-independence gate now covers the Anthropic variants.** The
  different-lab guard (an assertor must not be graded by its own family) used a
  *second, stale* lab table local to `falsify.py` that predated `sonnet` / `opus48` /
  `fable51`, so `assertor=fable` with `falsifier=sonnet` slipped through — Sonnet
  grading Fable, both Anthropic. Lab identity now comes from `oracles.lab_of()`, the
  single source of truth (which also merges gateway tokens), so a same-family pair is
  rejected regardless of which Anthropic voice is named.
- **A re-asserted `ask_falsify` claim can survive the round again.** A claim that was
  killed and then legally re-asserted with support that *addresses* the kill was
  reopened to `open`, but `resolve()` re-killed it the same round because the old
  accepted kill was still counted. Addressed kills are now marked and no longer count
  toward re-killing (history is kept); a fresh, unaddressed accepted kill in a later
  round still kills. A reopened claim also resets its failed-attack count, so it must
  re-earn `survived` with a new attack rather than coasting on its pre-kill tally, and it
  can be resurrected at most a few times before it stays dead (bounding kill→reopen loops).
- **Metamorph stability parsing no longer reads "UNTRUE" as "TRUE".** `_parse_verdict`
  scanned for `TRUE`/`FALSE`/`UNSURE` as substrings, so a judge replying `UNTRUE …`
  (or `NOT TRUE`) matched `TRUE` and certified a refuted claim as stable. It now matches
  on word boundaries and resolves a negated affirmative to `FALSE`.
- **`ask_falsify` metamorph no longer paraphrases with the assertor itself.** The
  metamorphic check rewords a claim and re-asks the asserting model cold; the
  reworder must be a *different* model or the check is trivially stable. It used to
  pick a cheap cross-lab model (minimax/deepseek/glm) and **fall back to the assertor**
  when none was configured — so on an OAuth-only box the assertor paraphrased and
  re-judged its own words, a useless check. The paraphraser now prefers a new internal
  **Haiku worker** (`ask_fable.worker`, Claude Haiku 4.5 on the same OAuth session —
  always available, cheap, always a different model than any assertor), falls back to a
  cross-lab model, and returns nothing (skip the round) rather than ever self-perturb.
  Haiku is a UTILITY worker, deliberately **not** an oracle: it shares Fable's/Opus's
  lineage so it adds no council diversity, and it is not in `KNOWN` / not reachable as a
  `models=[...]` token.

- **Redaction no longer swallows token prose in answers.** `token` left the
  strict keyed-secret line pattern: a token assignment is redacted only when its
  value is quoted or a bare >= 16-char credential-shaped run, and only the value
  is replaced — the rest of the line survives. Answers about fencing tokens or
  token buckets stay readable (`token = 42`, `token = N`), while
  `password`/`secret`/`cookie`/`authorization` remain strict and known credential
  shapes (JWTs, provider keys) are still caught globally regardless of label.

### Added
- **More Anthropic models on the OAuth session — `ask_sonnet`, plus `sonnet` /
  `opus48` council & chain tokens.** Claude Sonnet 5 (`ask_sonnet`, single-turn)
  is a cheap, fast Anthropic voice for high-volume or lower-stakes turns; Opus 4.8
  (`opus48`, token only) is a pinned baseline for regression / A-B work against
  Opus 5, the way `fable51` pins a Fable id. Both ride the same OAuth session as
  `ask` / `ask_opus5` (no key, flat-plan), and both are registered table-driven in
  the new `anthropic_variants` module rather than as more one-off bridge clones.
  They are SAME-LAB variants, so they add no council diversity and are excluded
  from every tier preset (`_TIER_EXCLUDED`) — they earn a council seat only when a
  caller names them.
- **Council independence gate — quorum now counts labs, not models.** A council's
  `consensus`/`quorum` signal is an independence claim, but a panel of several
  Anthropic models shares training lineage, so their agreement is correlated, not
  independent. `ask_council` now reports `independent_labs` and downgrades a
  unanimous `strong` verdict to `partial` when the answering panel spans fewer than
  two labs, with a `recommended_next_action` that says why. New `oracles.lab_of` /
  `distinct_labs` back it; unknown gateway models count as their own lab, so the
  gate only ever downgrades and never fabricates independence.
- **Quota-aware hold in the circuit breaker (opt-in).** A `rate_limit` (429 /
  quota — already classified by `http_error_detail` / `cli_error_detail`) is the
  provider saying "wait until T", not "chronically failing". With
  `ASK_FABLE_QUOTA_HOLD=<seconds>` set, a rate_limit now sets a separate, escalating
  per-oracle hold (exponential ×2 up to 1h, ±10% jitter) that `should_skip` honors,
  and does **not** feed the health window — so `state()` stays pure-health and
  `diagnose` reads a true signal. A quota reset (any success) lifts the hold. The
  hold is monotone (only ever adds a skip, `max()` never shortens) and the skip is
  reported with kind `circuit_open` but worded as a quota hold with the remaining
  time. Default off (`0`): a rate_limit feeds the window and trips exactly as before,
  and a stale hold is ignored — byte-identical to a build without the feature.
- **Per-source answer digest on the trace.** Every oracle's raw answer is now
  sha256'd onto its `provider.completed` trace event (a new additive `output` block:
  `sha256`/`chars`/`bytes`/`redaction_count`, hash-only in safe mode). The hash is
  computed once at the trace choke point (`record_provider`), not on
  `ProviderTelemetry`, over the answer text only — never `thinking` — so identical
  answers hash equal and a changed answer differs. This gives per-source signals the
  existing aggregate `output.sha256` can't: cross-source false-consensus / aliasing
  detection (two "independent" oracles returning byte-identical text is one vote) and
  diff-localization across runs. Redaction is deterministic, so equality holds;
  `redaction_count` flags when a digest is over redacted rather than raw bytes. The
  trace `schema_version` stays 2 (the field is additive and optional).
- **`diagnose` — a read-only health check of every reasoning backend.** For each
  oracle it reports reachability, the resolved model, the configured timeout, the
  circuit-breaker gate (`state` + `skip_reason` + `resume_in_s`), and a `fix:` line
  for anything down, rolled up to `ok` / `warning` / `error` (a backend that is
  unconfigured by design is `not_configured`, never `error`). It makes **no** paid
  model call and **never perturbs state** — it only checks a CLI's presence and a
  bounded `<cli> --version` (its own process group, killed on timeout so a hung CLI
  can't wedge the tool), whether an API key is set, and the breaker's new read-only
  `Breaker.snapshot()`; a contract test asserts a full run leaves `breaker._states`
  byte-identical and never calls `oracles.run`. The gate shape is forward-compatible
  (`skip_reason` gains `quota_hold` once the quota-hold feature is enabled).
- **LM Studio as a local backend — `ask_lms` + `list_lms_models`.** Ask a model on
  the operator's LAN LM Studio server (default `http://ai.home:1234`,
  `ASK_FABLE_LMSTUDIO_BASE_URL`) with no cloud key. A model that is not loaded is
  loaded EXPLICITLY — never via JIT — so LM Studio's Auto-Evict never unloads a
  resident model to make room, and `context_length` is always sent
  (`lmstudio_context` / `ASK_FABLE_LMSTUDIO_CONTEXT`, default 32768, raised to fit
  the prompt, capped to the model max) so a long prompt is not silently truncated
  by the app-wide 4k default. When it genuinely does not fit — a memory failure on
  load, or a resident instance loaded at too small a context — the swap path runs
  only under `lmstudio_swap=auto` (default): retry at a smaller context first,
  then unload blocking instances ONE AT A TIME (never a model with a chat in
  flight) with a wait for each unload to be confirmed gone and the load to be
  confirmed present, and best-effort RESTORE whatever was unloaded at its
  previous context if the retry still fails. Results report
  `swapped`/`unloaded`/`displaced`/`loaded_context_length`; `lmstudio_swap=never`
  turns those cases into loud errors instead. Loads are serialized by a
  process-wide thread lock AND an `fcntl` file lock (one ask_fable process per
  MCP session, so the file lock is what stops two sessions creating two
  instances), local output is capped at `lmstudio_max_tokens` (default 8192 —
  local generation is slow, so an uncapped draft must not run for minutes and
  blow a chain's client timeout), a suspected truncation is flagged
  `kind="truncated"` and never cached, and `lmstudio:<model>` is accepted as an
  explicit oracle token in `ask_chain` / `ask_council` — deliberately not part
  of any tier preset.
- **LM Studio model management — `unload_lms_model` + ask-first loads.** `ask_lms`
  no longer unloads a resident model by default: a load that does not fit returns
  a structured `unload_offer` naming the resident model(s), their sizes and the
  `unload_lms_model` tool, and the agent offers the choice to the operator.
  `lmstudio_swap=auto` opts back into the automatic unload+retry path (busy
  guard, wait-for-confirmation and restore-on-failure included). The new
  `unload_lms_model` tool frees one model on explicit operator request, refuses
  while that model is answering an ask_lms call, waits for the unload to be
  confirmed, and reports the freed bytes; `list_lms_models` now reports each
  loaded model's `size_bytes` and the total `loaded_bytes`, and the tool
  descriptions state the operator-consent contract.
- **`ask_lms_council` — a local-model panel, synthesized by Fable.** Ask several
  LM Studio models the same question, ONE AT A TIME (a single GPU serves them
  sequentially; each member we load is freed before the next, so a panel larger
  than VRAM completes and the box is left as found — a local synthesizer is
  freed too). The default panel is the five fastest strong locals from the
  2026-09-13 sweep (`qwen/qwen3.6-35b-a3b`, `qwen/qwen3.8-27b`, `qwen/qwen3.5-9b`,
  `zai-org/glm-4.6v-flash`, `google/gemma-4-31b-qat`), configurable per call with
  `models` or persistently with config `lmstudio_council` /
  `ASK_FABLE_LMSTUDIO_COUNCIL`. Fable synthesizes by default; any council token
  (`synthesizer=`) replaces it, including `lmstudio:<model>` for a fully local
  panel.
- **Control-panel integration — `host_status` + a real VRAM room check.** The
  ai.home control page now serves `GET /api/state.json`; ask_fable reads it
  best-effort via config `control_page` / `ASK_FABLE_CONTROL_URL` (default
  `http://192.0.2.10:5000`). The new read-only `host_status` tool reports GPU
  utilization, VRAM used/total/free, temperature, fan and power, the processes
  holding VRAM, service states, the loaded LM Studio models, and warnings. The
  same reading powers `ask_lms`'s pre-load room check, which distinguishes three
  cases: `fits` (load), `needs_room` (fits the GPU's total but not the free
  VRAM — the ask-first policy returns an `unload_offer`; auto swaps), and
  `too_large` (exceeds total VRAM, so unloading cannot help — refused as
  `model_too_large` with no misleading offer). `list_lms_models` classifies
  every loadable model into `fits_now` / `needs_unload` / `too_large` against
  the live GPU, so an agent never offers a model that cannot fit at all; an
  unreachable page falls back to attempt-then-offer.

## [0.15.0] - 2026-09-13

### Added
- **OpenRouter measured against the Atlas gateway wall: none.** A 16,384-token
  non-streaming `deep` call (`qwen/qwen3.8-2.4t-a95b`) returned HTTP 200 after
  314.1s (`finish_reason=length`, no retry, $0.10), so the only cap on a long
  OpenRouter call is ask_fable's own preset timeout. Documented in the
  `ask_openrouter` tool description, the help setup topic, the guide/tool docs
  and CLAUDE.md; the gotcha that a reasoning model can spend the whole output
  budget thinking and return `empty response from model` is noted too.
- **Atlas gateway-wall guidance + per-call cap/timeout overrides.** Atlas's
  gateway returns HTTP 504 for any chat request still generating at ~242s, so the
  `deep` preset's 16k tokens can die mid-generation while `standard` fits the
  window (every observed failure clusters at 242.5-243.6s across models; the
  client timeout never fires first on `deep`). Two additive config knobs —
  `atlas_max_tokens` / `atlas_timeout` (env `ASK_FABLE_ATLAS_MAX_TOKENS` /
  `ASK_FABLE_ATLAS_TIMEOUT`) — now replace the effort preset's output cap and
  wall-clock per Atlas call (single, panelist, or synthesizer), still bounded by
  `ASK_FABLE_MAX_TOKENS`. The `ask_atlas` tool description, the `ask_atlas_council`
  description, `ask_fable_help`'s setup topic, and the configuration reference
  state the ~242s wall explicitly so an agent can pick an effort that fits it.
- **LAN context bus — composite writer attribution (`machine/client[/model]`).**
  A sealed blob's `writer` id now records the MCP client that wrote it (e.g.
  `workstation/claude-code`), not just the machine. The client (`claude-code`,
  `opencode`, `codex`, …) is auto-detected from the same resolution the hub uses
  and omitted rather than written as `unknown`; a model component is appended
  only when explicitly configured via `ASK_FABLE_MODEL_ID` / config `model_id`,
  since no source can auto-detect the driving model. Each component is sanitised
  to a `/`-safe token and the whole string stays inside the AEAD associated data,
  so every field is authenticated together; a pre-composite bare-machine writer
  still parses as a one-element split, so no envelope version bump or daemon
  change was needed. The daemon's per-writer row cap now keys on the full tuple.
- **LAN context bus — reader-side rollback guard (stale-owner detection).** Each
  client now keeps a local per-key high-water mark of the authenticated envelope
  timestamps it has accepted (`context_hwm.db`, never on the bus). A bus value
  more than 60 s older than the mark is refused as a possible stale or
  rolled-back owner; writes advance the mark and a client-side delete clears it.
  Bus-mode reads also require a sealed envelope, so an owner cannot bypass the
  guard by serving plaintext. Deliberate restores opt out with
  `ASK_FABLE_CONTEXT_HWM=0` (or by deleting the guard database).
- **LAN context bus — blob codec + rollback guard (slice 1 of the owner-daemon
  plan).** The context bus can now be shared across machines: blobs are sealed
  client-side as `afctx1:` envelopes (ChaCha20Poly1305 + HKDF-SHA256 with a
  fresh salt and nonce per seal, storage key name and writer/timestamp bound
  into the AEAD associated data) under a fleet keyring
  (`ASK_FABLE_CONTEXT_KEYRING`, 0600, `kid:base64` lines, newest first, kid
  rotation without a flag day). `context_store` gains a backend seam and a read
  guard: any sealed value found in a store is unsealed with the keyring, and a
  value that cannot be opened **degrades** the store (naming the key) instead of
  splicing base64 armor into a prompt. With `ASK_FABLE_CONTEXT_BUS` set but the
  LAN client not yet shipped, all context ops fail closed — never a silent local
  fallback. Design and wire contract: `docs/context_bus.md`.
- **LAN context bus — client, daemon, and migration (slice 2).** The bus is now
  usable end to end. `context_bus.py` speaks to the daemon over a unix socket or
  HTTP with a 5 s timeout, authenticates every request with a separate bus token
  (`X-AF-Auth: ts.nonce.HMAC(token, method|path|ts|nonce|sha256(body))`), and
  cross-checks the daemon's claimed writer/timestamp against the AAD-bound
  envelope header — a mismatch degrades instead of returning mislabelled data.
  `context_busd.py` is the single owner of the bus database and holds no content
  key: it applies per-writer retention, a **persistent** anti-replay floor whose
  input is derived from the authenticated envelope header (a request whose
  metadata disagrees with the seal is rejected), age-evicted nonce replay
  defense, constant-time auth with clock-skew reporting, unix-socket default
  (0600, peer-uid checked) with token-required non-loopback TCP, and an
  unauthenticated `/health`. `context-busd migrate` seals an existing local
  `context.db` onto the bus. Install with the `ask-fable[lan]` extra.
- **`ask_falsify` — a stateful falsification ledger, the process cousin of
  `ask_debate`.** An assertor states typed claims; a falsifier (forced to a
  DIFFERENT lab) attacks them; and a deterministic CODE clerk — not a model —
  decides commit/kill/survive from receipts it verifies mechanically: a `cite`
  quote's verbatim presence in `context`, or a `contra` edge to a survived claim.
  A claim may speak, but it cannot compound (move reputation, count as consensus,
  survive) without a verified receipt, so a fabricated or absent quote dies. State
  PERSISTS across calls under the required `session` key — a killed claim stays
  dead and calling again continues the same ledger. `assertor` / `falsifier` /
  `rounds` (1–6 assert→attack cycles per call) are configurable; the result reports
  the survived / killed / open / crucible split plus per-model reputation. v1
  verifies `cite` and `contra` only (no code execution).
- **`ask_falsify` gains sandboxed `run:` receipts (opt-in, default OFF).** With
  `ASK_FABLE_ALLOW_RUN=1` and `bwrap` installed, a claim can be backed — or an
  attack can kill — by executing a model-authored Python snippet in a locked-down
  bubblewrap sandbox (no network, no filesystem, cleared env, memory/CPU/PID/tmpfs
  limits) instead of only citing the corpus: exit 0 supports a claim, a falsifier's
  non-zero test kills it, and a timeout / limit / missing module is *inconclusive*,
  never a refutation. This is the first receipt that manufactures evidence rather
  than retrieving it (a `run` kill clears the arena's `grep_ratio` frontier bar).
  Isolation flags were reviewed by a 3-model council; layered `systemd-run --user
  --scope` → `prlimit` → `bwrap` where available.
- **Bounded output for the `run:` sandbox.** `cli_gate` gained an opt-in
  `max_output_bytes` reader that caps retained stdout/stderr *as it streams* (default
  unchanged for every other CLI bridge); the sandbox uses it so a snippet that prints
  without bound can't grow the server's memory before the wall-clock kill.
- **`ask_falsify` gains `metamorph:` stability checks (opt-in `metamorph: true`).**
  The clerk restates an unsupported claim in a semantics-preserving way and re-asks the
  assertor COLD: a claim that flips is *unstable* and can't compound; one that reaffirms
  earns WEAK support — the only way a claim survives on a topic with no corpus to cite and
  no code to run. Instability is a proof of non-reasoning, never of falsity, so it withholds
  support but never kills; stability is not truth, so metamorph-only survivors are reported
  separately as `stable_unverified`. Costs two extra (cheap) model calls per unsupported claim.
- **Calibration store (foundation for calibrated councils).** `ask_falsify` now records each
  resolved claim's outcome (survived = right, killed = wrong) into a durable per-(model, domain)
  reputation store (`reputation.py`, SQLite, shrunk accuracy score; path override
  `ASK_FABLE_REPUTATION_PATH`). Anchored to resolved outcomes only — never peer agreement.
  Behavior-neutral for now; a later slice weights `ask_council` synthesis by it.

## [0.14.0] - 2026-09-10

### Added
- **`ask_conference` — a divergent, multi-round brainstorm across models.**
  Unlike a council (isolated answers) or a chain (sequential refinement), the
  participants argue a topic TOGETHER — each reads the running transcript and
  builds on or pushes back, then a rapporteur writes the map of the disagreement.
  A blind commit-then-reveal first round keeps early answers from anchoring;
  `models` / `rounds` / `synthesizer` are configurable (round cap 10), and with an
  elicitation-capable client a native model picker opens when `models` is omitted.
- **Every tool now advertises MCP `ToolAnnotations` hints.** All 39 tools carry
  explicit `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`
  booleans plus a human-readable `title`, so hosts (Claude Code, OpenAI's tool
  directory) can tell a safe read (`stats`) from a paid model call (`ask`) or a
  data-losing op (`context_delete`, `reset_session`) and prompt accordingly. Paid
  `ask_*` calls are deliberately `readOnly=false` (they append a trace, bump stats,
  spend money, and are non-deterministic) so a host never silently auto-runs one.
  These are advisory UX hints, not a security control.

### Changed
- **`ask_deepseek` tracks DeepSeek V4.1.** The default DeepSeek model follows the
  provider's rename (`deepseek-v4-pro` retires 2026-09-14), so calls resolve to
  the current V4.1 model instead of a retiring id.

## [0.13.1] - 2026-09-10

### Fixed
- **Cost reporting no longer mixes billing bases.** `stats(by="provider")` was
  summing flat-plan (Fable/Opus) subscription *list prices* into real per-token
  spend. It now keeps `cost_usd` (real billed) separate from `cost_usd_notional`
  (list-price reference) and reports `cost_covered`, so a billed provider that
  emits no cost reads as unpriced rather than free.
- **Multi-model verdicts stop overstating their authority.** A debate whose
  adjudicator failed no longer reports `resolution:"adjudicated"` — it returns
  `adjudicator_unavailable` with downgraded confidence (the answer is the
  proposer's own). A council reports `consensus:"strong"` only when the full
  *requested* panel answered and agreed, not just the survivors.
- **`context_ref` fails loudly instead of answering blind.** A degraded context
  store (locked/corrupt) is distinguished from an absent key (`store_degraded` vs
  `needs_context`), and a malformed `context_ref` is rejected rather than silently
  dropped (the hand-rolled validator now checks `anyOf` fields).
- **`configure_tracing` governs disk writes.** The trace-bundle writer and answer
  saver read `trace_mode` from config-over-env like everything else, so a runtime
  `safe`/`full` toggle actually takes effect.
- **Redaction covers every persistence sink.** The trace event store's error
  detail, bare provider keys on their own line, and the cross-agent hub's stored
  question/answer are all redacted now.
- **Concurrency and lifecycle.** A per-session lock stops two concurrent
  same-session `ask` calls from forking the conversation; `config.save` holds an
  exclusive lock so concurrent instances don't clobber each other's fields; a CLI
  subprocess is always killed and reaped on any mid-run failure or cancellation.
- **`trusted` is operator-gated.** The `trusted=true` flag that puts the
  prohibited-use denylist in log-only mode now takes effect only when the operator
  sets `ASK_FABLE_ALLOW_TRUSTED`.

### Changed
- **Harness portability is now a first-class claim.** The README, plugin tags,
  PyPI keywords, and GitHub topics name OpenCode, Kimi Code, Grok, Cursor, and
  Codex as MCP *hosts* (not just model backends). Registration snippets for
  Kimi Code (`~/.kimi-code/mcp.json`, including the host-timeout footgun) and
  Grok (`~/.grok/config.toml`) sit next to the existing Claude Code / OpenCode
  blocks. The compatibility surface is any client that can spawn a local stdio
  MCP server; Fable still rides the Claude Code OAuth session regardless of
  which harness is calling.

## [0.13.0] - 2026-09-07

### Added
- **`ask_fable_help` — the manual the standing instructions could not carry.**
  Claude Code truncates an MCP server's `instructions` field at ~2 KB. The old
  `SERVER_INSTRUCTIONS` was 7,747 chars, so agents received only its first 26% —
  the tool catalogue — and the operating manual behind it (how to frame a
  question, session hygiene, reframe-don't-retry, the context bus, council
  setup, response shapes) was cut
  mid-sentence and silently dropped.

  `SERVER_INSTRUCTIONS` is now 1,981 chars and carries only what exists nowhere
  else: when to reach for a tool, the double-strike rule, the instruction to tell
  the user an oracle is an option even when not calling one, the "models cannot
  open files" rule, session hygiene, and a pointer to the new tool. A test pins
  the budget so it cannot silently regress.

  The overflow moved into `HELP_TOPICS`, served by a new **`ask_fable_help(topic)`**
  tool — free, local, instant, no model call — with topics `refused`,
  `context`, `setup`, `tools` and `all`. Progressive disclosure: a small resident
  pointer, detail pulled on demand.

  Guard refusals now also carry a **`how_to_reframe`** field, so the
  reframe-don't-retry recipe arrives at the one moment it is actionable instead of
  depending on an instructions block the agent never received.

- **`twin` — the "twin flames" model group.** One operator token that expands to
  BOTH Anthropic reasoners, `fable` + `opus`, wherever a *list* of models is
  taken: `ask_council(models=["twin"])` or `ask_council(tier="twin")` fans out to
  the pair in parallel, `ask_chain(pipeline="m3 > twin")` expands in place to two
  stages (fable drafts, opus decides). Spelled `twin`, `twins`, `twin flames`,
  `twin-flame` or `twin_flames`. Both members ride the same OAuth session as
  `ask`/`ask_opus5`, so the pairing needs no provider keys and is the cheapest
  real second opinion the server can give.

  A GROUP is a new kind of token, distinct from an ALIAS: an alias renames one
  model, a group takes more than one seat. Single-model slots — `synthesizer` on
  every council, and `proposer`/`opponent`/`adjudicator` on `ask_debate` — refuse
  a group with a `bad_args` error naming its members, rather than resolving to
  whichever one sorts first and dropping the other silently.

  `GROUPS`/`GROUP_ALIASES` are validated at import (`_validate_groups`). Group
  expansion is a macro over the caller's list, so a malformed definition doesn't
  raise — it silently changes what gets asked: an *empty* group vanishes and the
  fan-out falls through to the DEFAULT council with an empty `unknown`; an
  *unknown member* is reported under its own name, telling a caller who typed
  `twin` that `nope` is unknown; a *nested* group is never expanded (expansion is
  deliberately single-pass) and lands in `unknown`. All three are config edits
  rather than runtime inputs, so they now fail loudly at import instead of being
  absorbed by defensive branches in two resolvers.

## [0.12.0] - 2026-09-03

### Added
- **OpenRouter as a first-class provider** — four tools mirroring the Atlas set
  (`ask_openrouter`, `list_openrouter_models`, `ask_openrouter_council`,
  `configure_openrouter_council`) plus `openrouter:<model-id>` tokens usable
  anywhere `atlas:` is: council member or `synthesizer`, chain stage, debate
  proposer/opponent/adjudicator. One API key reaches ~400 models from every
  major lab, which makes a genuinely cross-LAB council possible without
  configuring each provider separately. Set `ASK_FABLE_OPENROUTER_API_KEY` (or
  `OPENROUTER_API_KEY`); the catalog endpoint needs no key at all.

  Three things are deliberately *not* copied from Atlas:
  - **Effort is clamped, not probed.** OpenRouter publishes each model's
    `supported_efforts`, so `deep` asks for the most that model accepts and
    omits the field for non-reasoning models. Atlas has to send a guess and
    retry without it on a 400 — one wasted round trip per call to rediscover
    something a free catalog already states.
  - **Ranking reads the catalog, not a keyword table.** `recommend_models`
    scores reasoning support, context length, price and release date, so a
    model released today ranks correctly with no change here. Price counts
    *for* a model unless the task asks for cheap — nobody charges $50/M for a
    weak model, and ranking on cheapness by default put a tiny free model at
    the top of an engineering oracle's menu.
  - **Cost is reported, not estimated.** OpenRouter returns the real dollar
    cost and the upstream that served the call, so `ProviderUsage.cost_usd` is
    measured rather than derived from a price table.

  Grok and Kimi ids reroute to the operator's local `grok`/`kimi` CLIs, as they
  already do for Atlas, so a model you can serve for free is never billed per
  token.
- **`docs/OPENCODE.md` covers OpenRouter in opencode** — and says to use
  opencode's native `/connect` rather than a manual provider block. Verified on
  opencode 1.18.23: the native provider lists ~350 models and serves real
  requests, keeps the key in the credential store instead of duplicating it into
  `opencode.json`, and carries per-model cost/context metadata a hand-written
  `@ai-sdk/openai-compatible` block cannot. That is the opposite of the Atlas
  section, where no native provider exists and the manual block is the reliable
  path — so the two must not be copied from one another.
- **Circuit-breaker transitions are visible.** A trip, a failed half-open probe
  and a recovery each print one `⚠ circuit breaker opened|reopened|closed for
  <oracle>` line to stderr and land as a `breaker.<transition>` event in the
  trace log, carrying the error rate, window and cooldown that decided it, plus
  a `provider` block so `trace_list(provider=…)` finds the trace it happened in.
  Until now the breaker changed state in silence: the only symptom was
  `circuit_open` in a later council's `sources`, with nothing to say when it
  tripped or why.

### Fixed
- **Reported cost no longer adds real dollars to subscription list prices.**
  `usage.cost_usd` summed every provider indiscriminately, but the two are
  different currencies: a per-token gateway reports money actually charged,
  while an OAuth-backed Anthropic oracle reports a LIST price for work a flat
  plan already covers. A council mixing them produced a figure that was neither
  — measured on a real run, one reported ~$0.121 when the amount actually
  billed was $0.00105, a 114x overstatement presented as a bill.

  `ProviderUsage` now carries a `cost_basis` (`billed` | `subscription`), and
  the aggregate keeps them apart: `cost_usd` is what you pay,
  `cost_usd_notional` is what the plan absorbed. An unlabelled cost still counts
  as billed, so the number can overstate a bill but never understate one.
  All-billed runs report exactly what they did before.
- **An oracle cancelled by a wall-clock cap now counts in `stats`.**
  `stats(by="provider")` is the one view that sees council, chain and debate
  members individually, and it reads the per-call `provider.completed` event —
  which a member cancelled by `ASK_FABLE_COUNCIL_TIMEOUT` /
  `ASK_FABLE_CHAIN_TIMEOUT` (or whose bridge broke its never-raise contract)
  never emitted, because cancellation stops the coroutine before it records
  anything. The exact failure the view exists to catch — an oracle that only
  fails when it runs long — was the one it could not see. `oracles.run` now
  records the attempt itself, so the invariant holds for all three
  orchestration modes rather than one. A cancelled call is deliberately not fed
  to the circuit breaker: our own impatience is not evidence of backend
  ill-health. Provider events also carry the error `kind` (`timeout`,
  `cancelled`, `circuit_open`, …), as `tool.completed` already did — a capped
  member reads as `cancelled` in its own event and `timeout` in the
  orchestrator's `sources`, because the oracle cannot know which cap stopped it.
  A skipped call reports `transport="skipped"`, so a shed call is no longer
  indistinguishable from one that reached a backend.
- **Breaker-shed calls no longer count as backend errors.** A `circuit_open`
  skip touches no backend, yet it landed in `stats` as an error with ~0 ms
  latency — so for the length of every cooldown a tripped oracle looked both
  worse and faster than it was. Shed calls are now reported under a separate
  `circuit_open` count and excluded from calls, errors and latency — reaching
  `totals` even under `by="model"`, which cannot bucket a call that has no model
  and would otherwise report a shed-only window as no traffic at all. The `stats`
  console line reports them too, instead of printing `avg Nonems`. Because
  `error_rate` is computed over calls, a bucket that was shed all window would
  read `0.0` however dead it was, so shed calls carry their own `shed_rate`, and
  they still count toward the sort so the dead backend does not sink below the
  healthy ones. A shed call reports `transport="skipped"`.
- **A call a local cap cancelled is no longer counted as a backend error.**
  `stats` gives it its own `cancelled` count: the latency is real but the
  failure was ours, which is the same reason the circuit breaker already ignores
  cancellations. Without this a slow-but-healthy oracle repeatedly capped by a
  council climbed to `error_rate: 1.0` while the breaker correctly stayed shut.
- The `stats` tool's `by` description listed three of its eight bucket keys;
  `provider` — the per-member view — was not among them.

### Changed
- **Shared OpenAI-compatible plumbing extracted to `openai_compat.py`.** The
  error envelope, `choices[0]` extraction, finish-reason lookup, effort ladder,
  WAF User-Agent and output cap are identical for any such gateway and are
  exactly the code that gets patched after an incident. A pure move:
  `atlas.py` re-imports each name under the identifier it already used, so every
  `atlas.*` attribute path and existing monkeypatch still resolves.
- **`grok.looks_like_grok_model` accepts both vendor spellings.** Atlas writes
  `xai/`, OpenRouter writes `x-ai/`; only stripping the first meant an
  `x-ai/grok-*` id quietly billed through the gateway while the operator's
  already-authenticated CLI sat idle.

## [0.11.0] - 2026-09-02

### Added

- **Fable is no longer pinned to a model id.** `ask` now asks for the newest
  Fable in `fable.FABLE_CANDIDATES` (currently `claude-fable-5-1`, then
  `claude-fable-5`) instead of the hardcoded id it was written with. If a
  transport rejects the preferred id outright, that id is demoted for the life
  of the process and the turn is retried one rung down — so a machine whose
  Claude Code is too old degrades to Fable 5 and answers, rather than failing.

  `ASK_FABLE_FABLE_MODEL` pins an exact id and skips the ladder entirely.
  A pinned call never falls back: naming a model and silently being answered by
  another would make an A/B, or a trace that records the model, a lie.

  New `fable51` council/chain/debate token (aliases `fable5.1`, `fable-5.1`,
  `claude-fable-5-1`) pins claude-fable-5-1 and keeps meaning it after the
  ladder moves on. It is deliberately left out of the `middle`/`full` tier
  presets: it is the same tier, price, and — today — the same model as `fable`,
  so a blanket fan-out including both would pay twice for one voice.

- **Claude Opus 5 as a first-class oracle** — a new `ask_opus5` tool and an
  `opus` model token (aliases `opus5`, `opus-5`, `claude-opus-5`) usable
  anywhere `fable` is. `ask_opus5` is the multi-turn twin of `ask`: identical
  arguments, identical result shape, same Claude Code OAuth session (Agent SDK
  with the `claude` CLI as fallback) — Opus 5 is roughly half Fable's price and
  faster, so it fits high-volume and long back-and-forth work while `ask` stays
  for the hardest calls. `opus` also works as an `ask_council` member or
  `synthesizer`, an `ask_chain` stage, and an `ask_debate`
  proposer/opponent/adjudicator, and it joins the `middle`/`full` council tiers
  (the `default` tier is unchanged). Sessions are namespaced per tool, so the
  same `session` key on `ask` and `ask_opus5` is two independent conversations —
  an SDK session id belongs to the model that created it, and a cross-model
  resume would silently swap models mid-thread.
- **`ask_debate` accepts an `adjudicator`** — the ruling model was hard-wired to
  Fable; it is now any council token (default `fable`, e.g. `opus` or `codex`),
  so the debating pair and the judge can be chosen independently. A non-default
  adjudicator joins the cache key; the default key is unchanged.
- **`reset_session` accepts a `model`** (`fable` | `opus5`, default `fable`) so
  an `ask_opus5` conversation can be dumped and cleared from its own namespace.
- **`ask_council` accepts a `synthesizer`** — any council token ('codex'/'gpt',
  'atlas:<model-id>', 'ollama:<model>', …) can now adjudicate the panel instead
  of Fable. The chosen model's own panel answer is still anonymized and read
  last; an unavailable or failing synthesizer falls back to Fable (then to the
  first answer), and the new `synthesis` result block reports
  requested/used/fallback so degradation is never silent. A non-default
  synthesizer joins the outer cache key; the default key is unchanged.
- **`ask_atlas_council`** — the Atlas-only council with GPT-5.6 Sol as the
  default adjudicator: the local `codex` CLI when installed, else Atlas-hosted
  `openai/gpt-5.6-sol`, else Fable. Members default to the persisted
  `atlas_council` config (or `ASK_FABLE_ATLAS_COUNCIL`), else 3 featured
  catalog models (one per provider); `xai/grok-*` members reroute to the local
  `grok` CLI keylessly, and an all-grok panel runs without an Atlas key.
- **`configure_atlas_council`** — persist the default Atlas council and/or its
  adjudicator (`atlas_council` / `atlas_synthesizer` config keys, env fallbacks
  `ASK_FABLE_ATLAS_COUNCIL` / `ASK_FABLE_ATLAS_SYNTHESIZER`), mirroring
  `configure_ollama_council`.

### Changed

- **Audited every roster surface against the live tool list.** `README.md`,
  `docs/GUIDE.md`, `CLAUDE.md` and the printable menu now mention all 33 tools:
  the README never documented `ask_kimi` at all, and GUIDE predated
  `ask_atlas_council`/`configure_atlas_council`. The skills gained `ask_debate`
  and `context_pack` — a first-class reasoning mode and the point-don't-paste
  bus, both of which they predated; ops tools are deliberately still omitted
  there, since a skill listing `trace_get` is noise to an agent choosing a model.
- **Corrected two documented defaults that had drifted from the code.**
  `ASK_FABLE_GROK_MODEL` is `grok-4.6` (not `grok-4.5`) and
  `ASK_FABLE_ATLAS_MODEL` is `xai/grok-4.6` — the superseded id also appeared in
  agent-facing `ask_atlas` examples, where a copied-verbatim id is a failed call.
- **Refreshed the skills and diagrams that still described an older roster.**
  `ubercode`/`uberplan`/`uberarch`/`uberbrainstorm` predated `ask_opus5`, Grok
  and Kimi, and `uberplan` still described the `middle` tier as
  `+glm+gemini+codex`. The architecture map and the hero image named a pinned
  `claude-fable-5`; the map now names the ladder (`newest claude-fable-*`) so it
  cannot rot again, and `hero-council.png` was re-rendered from its `.src.html`
  so the image and its source agree.
- **`audit.record(model=...)` no longer defaults to a pinned model id.** Every
  real caller passes the model that actually ran; the default only applied to
  tests, where a plausible-but-wrong id is worse than an explicit blank.

- **Default models refreshed to current releases** — the local `grok` CLI's own
  default had already moved to `grok-4.6`, but ask_fable pinned `-m grok-4.5`
  explicitly and so actively downgraded every `ask_grok` call;
  `DEFAULT_GROK_MODEL` is now `grok-4.6`. `DEFAULT_ATLAS_MODEL` likewise moves
  from `xai/grok-4.5` to `xai/grok-4.6` (same $2/$6 per M, newer). Both remain
  overridable via `ASK_FABLE_GROK_MODEL` / `ASK_FABLE_ATLAS_MODEL`.

- **One shared CLI runner** — the five copy-pasted spawn/kill protocols
  (claude/mmx/agy/codex/grok: own-session Popen, group-SIGKILL on timeout,
  bounded drain) are now a single `cli_gate.run_cli`, and the five per-file
  `_FakePopen` test doubles are one shared conftest fixture.

### Fixed

- **The Agent SDK ran a different Claude Code than the one on your PATH.** The
  SDK prefers a binary vendored inside `claude-agent-sdk` over PATH, and that
  copy only moves when the SDK is upgraded — here it was 2.1.205 against a
  2.1.258 on PATH. Since Fable 5.1 needs ≥ 2.1.251, the primary (SDK) transport
  refused a model the fallback (CLI) transport ran fine. `fable.best_cli_path()`
  now hands the SDK the PATH binary when it is strictly newer, leaving its own
  choice alone otherwise; `ASK_FABLE_CLAUDE_CLI` overrides both.
- **A failed SDK turn hid the only useful part of the error.** The API's own
  sentence lives on `ResultMessage.result` ("...does not support this model;
  version 2.1.251 or newer is required"), but the bridge reported the first
  error it saw — an `AssistantMessage.error` placeholder reading `"unknown"` —
  and every such failure surfaced as `Fable SDK request failed: unknown`. The
  result sentence now wins, which is also what makes the failure classifiable.
- **New `model_unavailable` error kind**, produced by both the CLI and HTTP/SDK
  classifiers for "this build cannot run that model". Like `auth_failed` it is a
  local-config state, so `health.py` exempts it from the circuit breaker —
  otherwise the actionable "update Claude Code" message would be replaced by
  `circuit_open` after a few turns.
- **`ask` credited the wrong model after a fallback.** The handler reported the
  id it resolved *before* the call, so a mid-call demotion left the payload,
  audit row, hub turn and saved transcript all naming a model that never ran.

- **Kimi Code as a local-CLI oracle** — a new `ask_kimi` tool and a `kimi` model
  token, served by the local `kimi` binary on the operator's Kimi Code
  subscription. `atlas:moonshotai/kimi-*` tokens (including `ask_atlas_council`
  members) now reroute to it automatically when the binary is installed, exactly
  as `atlas:xai/grok-*` does for the `grok` CLI — no Atlas API key and no
  per-token billing. Atlas ids are mapped to local aliases (`moonshotai/kimi-k3`
  → `kimi-code/k3`, the k2.x line → `kimi-code/kimi-for-coding`); an id with no
  mapping stays on Atlas rather than silently answering as the default model.

  k3 is a 1M-context model but the CLI transport is not: it takes the prompt as
  one argv value, which the kernel caps near 131k bytes, so oversized prompts are
  refused with a `context_too_large` error pointing at the HTTP route.

  The CLI has no `--disallowed-tools` / `--system-prompt-override` /
  `--permission-mode` flags and its `-p` mode is fully agentic: asked a
  repo-flavored question it leaves the working directory, walks the real
  filesystem, and splices raw tool output into `--output-format stream-json`.
  Every turn therefore runs against a generated `KIMI_CODE_HOME` under
  `${XDG_STATE_HOME}/ask_fable/kimi-home/<effort>/` that copies the operator's
  providers/models, adds a deny-all `[[permission.rules]]` block, carries the
  scope contract as `SYSTEM.md` (a non-empty one replaces the builtin profile
  prompt), symlinks the login material — and pointedly does NOT link
  `workspace-trust`, which measurement shows is the guard that actually stops
  tool execution (trust+rules still ran 6 tool calls; no-trust+rules ran 0).
  A turn that executes a tool anyway is discarded as an error rather than
  returned, since its answer may reflect local reads instead of the caller's
  `context`.
- **`cli_gate` accepts a per-child `env` overlay** — merged over the server's
  environment for one spawn (used to point `KIMI_CODE_HOME` at the sandbox).

- **`ask_glm` survives losing the Z.ai subscription** — with no
  `ASK_FABLE_GLM_API_KEY`, the `glm` oracle now falls back to Atlas-hosted
  `zai-org/glm-5.3` on the Atlas key instead of reporting `not_configured`,
  keeping `glm` usable as a council/chain/debate member on one key. The direct
  Z.ai endpoint is cheaper so it still wins whenever its key is set, and the
  result keeps `key="glm"` for attribution. The two routes speak different
  protocols (Z.ai is Anthropic `/v1/messages`, Atlas is OpenAI-compatible
  `/v1/chat/completions`), so redirecting `ASK_FABLE_GLM_BASE_URL` at Atlas
  cannot work — this fallback is what bridges them. Only `glm` opts in;
  `deepseek` still reports `not_configured`.
- **Atlas model ids keep their casing** — Atlas ids are case-sensitive (e.g.
  `deepseek-ai/DeepSeek-V3.1-Terminus`, `Qwen/Qwen3-235B-…`), but every token
  path lowercased them, so mixed-case models failed with HTTP 400 "not found" —
  including featured-catalog models auto-picked by `ask_atlas_council`'s
  zero-config default panel. Names, aliases, and the `ollama:`/`atlas:`
  prefixes stay case-insensitive; the `atlas:` model part now travels verbatim,
  and de-dupes are case-insensitive keeping the first-seen spelling
  (`oracles.resolve`/`resolve_ordered`, `atlas.dedupe_models`,
  `ask_atlas_council` member normalization, and `synthesizer` resolution).

- **CLI gate waits are bounded by the call's own timeout** — the per-binary
  concurrency gate previously used an untimed semaphore acquire, so with both
  slots busy a call's wall time was queue-wait + timeout (unbounded as waiters
  stacked), and a council-cancelled caller parked in the queue still launched
  a full CLI run whose result was discarded (pure quota burn). The queue wait
  now consumes the call's budget (a starved call fails as a timeout naming the
  gate), and a cancelled caller never spawns.
- **`auth_failed` classification now covers CLI oracles too** — 0.10.0 added
  the auth kind (and its circuit-breaker exemption) only to the HTTP bridges,
  so a logged-out codex/grok/gemini/mmx still tripped the breaker and hid the
  actionable fix-your-login message behind `circuit_open`. `cli_error_detail`
  and `http_error_detail` now share one auth vocabulary.
- **Fable CLI/SDK failures surface their real error** — the CLI fallback
  discarded stderr and returned a constant "Fable CLI failed" (usage-limit and
  login errors were invisible and misclassified as `sdk_error`); it now routes
  through `cli_error_detail` like every other CLI bridge. The SDK error path
  likewise carried a constant text with a 0 ms stub telemetry; it now reports
  the actual error and keeps the real telemetry (request id, duration, usage).
- **`ask_council`'s declared models schema matches the handler** — it now
  accepts the documented `atlas:<model-id>` tokens and `m3`/`gpt`/`xai`
  aliases, so strictly-validating MCP hosts no longer reject calls the server
  supports (`oracles.ALIASES` is public now).
- **Transient retry no longer doubles a backend's wall-time budget** — the
  0.10.0 one-shot 429/5xx retry gave each attempt a fresh `timeout + 5`
  window, so a slow first attempt plus a retry could blow past the council cap
  (`ASK_FABLE_COUNCIL_TIMEOUT = timeout + 120`) and get the panelist cancelled
  as an opaque `kind:"timeout"`, masking the provider's real error. The three
  pasted retry loops (GLM/DeepSeek, Atlas, Ollama) are now one shared
  `oracle_common.call_with_transient_retry` with a single total deadline of
  `timeout + 5`; the retry fires only while a useful window remains, and a
  timeout message reports true elapsed time.
- **Retry attempts no longer inherit the previous attempt's telemetry** — a
  network failure after a retried 429 used to report the 429's provider
  request id; transport state is now cleared per attempt, and the parsed
  response body is kept separate from transport facts (a 200 body containing
  an `http_status` key can no longer forge a retryable status).
- **Atlas `deep` no longer re-POSTs permanent 400s** — the opportunistic
  `reasoning_effort` retry now requires the error to read like a rejected
  field (mirroring Ollama's `think` guard), so an oversized-context or
  invalid-request 400 costs one upstream request instead of two.
- **Retention pruning can no longer delete files ask-fable didn't write** —
  `prune_dir` now only considers filenames matching the writer's own shape
  (answers: `{tool}-{model}-{trace_id|stamp}.md`; sessions:
  `{slug}-{stamp}-{hash}.md`), so a user-pointed `ASK_FABLE_OUTPUT_DIR` or a
  shared state dir containing foreign Markdown is safe from the caps. Docs now
  state loudly that the default dirs are per-user shared, so one agent's cap
  prunes the shared archive.
- **A file vanishing mid-prune no longer aborts the whole round** — the stat
  pass is per-file best-effort (as the docstring always promised), so
  concurrent pruners can't silently disable each other's retention; the sweep
  also switched to `os.scandir`, halving stat syscalls.
- **Test suite is hermetic against exported retention caps** — a new conftest
  fixture strips `ASK_FABLE_MAX_ANSWERS`/`ASK_FABLE_MAX_SESSIONS`, which
  previously made `test_sessions` fail for operators with a cap exported.

## [0.10.0] - 2026-07-18

### Changed
- **Scope opened to conceptual/brainstorming questions** — the shared oracle
  system prompt (all 9 bridges) now explicitly allows ideation, "what could we
  build", and design questions for future code, with or without code context.
  The third refusal category ("not related to the agent's work") is gone;
  exactly two remain, and the offensive-security category now triggers only on
  a direct offensive ask in the question itself (exploit development, attack
  tooling) — questions about security-related code are answered, not refused
  (the deterministic denylist is unchanged).
  Generative questions get a menu of distinct ideas instead of one forced
  recommendation, and the council synthesizer merges brainstorms as a
  deduplicated union of distinct ideas rather than collapsing them. Tool
  descriptions and SERVER_INSTRUCTIONS unified to the same two-category
  wording (`ask_chain`/`ask_debate` previously said only "software/engineering
  only"). New `tests/test_prompts_scope.py` pins the policy.
- **Docs to reality** — install instructions no longer claim a PyPI package
  (`pip install -e .` / `pipx install .` from source), and the audit rotation
  naming is documented as it actually is (`decisions.<timestamp>.<seq>.jsonl`,
  not `decisions.jsonl.N`).

### Added
- **`ask_atlas` + `list_atlas_models`** — Atlas Cloud text models as oracles
  (needs `ASK_FABLE_ATLAS_API_KEY`), with quick/standard/deep effort presets
  and dynamic `atlas:<model-id>` tokens accepted in councils, chains, and
  debates.
- **`ask_grok`** — Grok via the local `grok` CLI (preferred over `ask_atlas`
  with `xai/grok-*` when the binary is installed), plus per-agent `agent_id`
  attribution and live-work-first hub dashboard defaults.
- **Cross-instance session hub** — visibility-only SQLite mirror of oracle
  turns across every agent on the machine: `session_list` / `session_peek` /
  `session_stats` tools, project-scoped by default.
- **Guard domain scope** — biology explicitly blocked via a bundled denylist
  (never silently upgraded from salient-core); neuro/cogsci/AI/CS allowed;
  an operator-authorized `trusted_session` flag; policy pinned by tests.
- **Task-aware Atlas model selection** — `list_atlas_models(task, limit,
  interactive)` ranks a provider-diverse shortlist from live catalog metadata
  and returns a native MCP form when supported or a structured `picker`
  fallback otherwise. Accepted selections carry the chosen model and effort;
  Atlas models can also be used as dynamic `atlas:<model-id>` tokens in
  councils, chains, and debates.
- **`ask_deepseek`** — standalone single-model tool for deepseek-v4-pro via
  DeepSeek's Anthropic-compatible endpoint (mirror of `ask_glm`; needs
  `ASK_FABLE_DEEPSEEK_API_KEY`).
- **Cheap-model-first preference** — the canonical oracle order (`oracles.KNOWN`,
  which `resolve()` applies to every council selection) is now cheap-first:
  fable, deepseek, minimax, glm, then the subscription-CLI models gemini and
  codex. Previously the middle/full tiers fanned out pricey-first despite the
  tier lists saying otherwise.
- **Availability-aware default council** — `oracles.default_models()`: the
  default council grows to fable+deepseek+minimax when
  `ASK_FABLE_DEEPSEEK_API_KEY` is configured (checked live per call, no
  restart), and stays fable+minimax otherwise. Applies to the `default` tier and
  to `ask_council` calls with no explicit `models`. The `ask_chain` default
  pipeline (`minimax > fable`) is unchanged — order is the computation there.
- **Transient-error retry** — the HTTP bridges (GLM/DeepSeek, Atlas, Ollama)
  retry once on 429/5xx/529 with jittered backoff; the attempt count lands in
  provider telemetry as `retry_count`.
- **`auth_failed` error kind** — 401/403 (or auth phrasing in the body)
  classifies distinctly from `sdk_error` and is excluded from the circuit
  breaker, so a bad key keeps surfacing the actionable "fix your key" error
  instead of `circuit_open`.
- **Council telemetry in stats** — quorum, consensus, and synth_fallback are
  recorded on v2 `tool.completed` events; `stats by="mode"` buckets by
  orchestration mode (council/chain/debate vs `?`) with per-bucket
  `consensus_counts` and `synth_fallback_true`.
- **Retention caps** — `ASK_FABLE_MAX_ANSWERS` / `ASK_FABLE_MAX_SESSIONS`
  prune the oldest saved answers / session dumps beyond N, only after the new
  file is durably written. Off by default.

### Fixed
- **Fable CLI process hardening** — the `claude` bridge now spawns in its own
  session, SIGKILLs the whole process group on timeout, and holds a `cli_gate`
  slot, so a hung agentic turn (a grandchild pinning the stdout pipe) can no
  longer wedge every later Fable call.
- **Council alias parity** — `oracles.resolve` applies the `m3`/`gpt`/`xai`
  aliases exactly as chain pipelines do; an aliased council selects the named
  model instead of silently falling back to the default panel.
- **`stats by="mode"` bucketing** — single-model calls land under `?`;
  previously they surfaced as a bucket literally named `None`, and after the
  first fix they leaked the trace *capture* mode (`safe`/`full`) from the v2
  event's top-level `mode` field (#31, #32).
- Reliability hardening across the oracle bridges: CLI stderr surfaced and
  rate limits classified, per-binary subprocess concurrency gate, grok agentic
  mode, doubled default timeouts, greppable trace errors, and coordination
  tools skipping bundle writes.
- Consensus `recommended_next_action` accuracy, audit-log test isolation, and
  multi-oracle hub session grouping.

### Security
- **SQLite sidecar permissions** — `serve()` sets a restrictive `0o077` umask
  at process birth and every store chmods the DB plus its `-wal`/`-shm`/
  `-journal` sidecars to `0600`. Previously the WAL/SHM files (holding the
  most recent hub Q&A text and context blobs) were created at the default
  umask as `0644`.

## [0.9.1] - 2026-07-12

### Added
- **`configure_tracing`** — toggle reasoning-trace capture at runtime, persisted to
  the config file: `trace_mode` (`safe`/`full`) and `stream_reasoning` (bool). Both
  **override** the `ASK_FABLE_TRACE_MODE` / `ASK_FABLE_STREAM_REASONING` env defaults
  (config → env → default), and — because both settings are read live per call — take
  effect on the next call with **no restart**, so reasoning traces can be enabled or
  disabled by asking instead of hand-editing `~/.claude.json`. A new
  `config.setting()` resolves that precedence for any string/flag setting.

## [0.9.0] - 2026-07-12

### Added
- Schema-v2 correlated MCP traces with guard, cache, provider, orchestration,
  synthesis, fallback, and artifact events; privacy-safe `trace_list` and
  `trace_get`; full-mode redacted trace bundles; and expanded usage/cache stats.
- **`consensus_votes`** on the council result — a per-recommendation tally with a
  `no_sidecar` bucket, so a `strong`/`partial` verdict can't hide that it rests on,
  e.g., 2 of a 5-oracle panel.
- **`store_error`** + resolved `db_path` on `context_read` / `context_list` when the
  context store is degraded, so a bad path/permission no longer masquerades to the
  agent as "your key doesn't exist" (which would make it re-paste the very context
  the store exists to hold). Backed by a new `context_store.last_error()`.

### Changed
- Answer Markdown now defaults off in safe mode and on only in full trace mode;
  `ASK_FABLE_SAVE=1|0` remains the explicit override.
- Rotated audit segments are retained without a limit by default; setting
  `ASK_FABLE_AUDIT_BACKUPS` opts into a finite cap.
- **Council consensus is now coverage-aware.** `_consensus` only reports `strong`
  when every panelist that answered also emitted a usable recommendation *and* they
  agree; a dropped/malformed sidecar makes it `partial`, not a false `strong`.
  All-`low`-confidence unanimity also downgrades to `partial`, and
  `material_disagreement` now fires on `apply` vs `investigate` (not only
  `apply`∧`reject`). `needs_more_context` is treated as an input abstention, not
  opposition. Consensus labels for the same panel can therefore differ from 0.8.x.
- The answer cache key is now versioned (`cache._KEY_VERSION`), so a payload/semantics
  change invalidates the whole cache instead of serving stale-shaped answers. Bumped
  for the consensus change above, so pre-0.9.0 council results are re-computed.

### Fixed
- Denylist inflection bypass: plurals/gerunds slipped the guard because the word
  boundary sat right after the base term (one suffix defeated the whole list). A
  per-word `_inflectable` helper now covers simple inflections and e-final stems'
  drop-e forms (`exfiltrate` → `exfiltrating`/`exfiltrated`), mirrored on the benign
  allowlist so plural benign phrases (`request payloads`) are still scrubbed.
- Context store never-raise masked misconfiguration: `get() → None` conflated "key
  absent" with "store degraded", and the post-connect write-failure path recorded no
  error at all. Failures are now noted and surfaced via `store_error`.

## [0.8.0] - 2026-07-10

### Added
- **`ask_debate`** — an adversarial third mode alongside `ask_council` (parallel
  vote) and `ask_chain` (sequential relay). Two models argue over a structured
  **claims ledger**: a `proposer` decomposes its position into load-bearing claims,
  an `opponent` disposes of each (concede, or contest with a concrete failure
  scenario), the proposer revises under fire, and a fresh **anonymized** Fable
  adjudicates on the merits. The outcome is decided **deterministically server-side**
  from the ledger — `resolution` is `conceded` / `converged` / `adjudicated` /
  `stalemate` (which mechanically downgrades confidence) — never model self-report.
  Pick the pair with `proposer`/`opponent` (e.g. `opponent="codex"` for Fable vs
  GPT-5.6 Sol, or `"glm"`); `rounds=2` adds a rebuttal pass with stalemate detection.
  Degrades to a single-critic pass when the opponent is unconfigured. The ledger rides
  a **separate `json-debate` block** (new `debate_ledger` module) so it never perturbs
  the slim `json-sidecar` decision contract or any existing tool. Full transcript is
  written to disk; the reply is a decision plus a compact `debate` block
  (`resolution`, `decisive_argument`, `recommendation_drift`, `low_effort_opposition`).
- **`ask_codex`** tool + **`codex`** council oracle — OpenAI's `gpt-5.6-sol` as a
  guarded, single-turn reasoning oracle, reached by shelling out to the local
  `codex` CLI in non-interactive `codex exec` mode (no API key; reuses the host
  `codex login`, exactly like the `mmx`/`agy` bridges). The invocation is hermetic
  and read-only (`--ignore-user-config --sandbox read-only --skip-git-repo-check
  --ephemeral`), so the operator's own Codex config/hooks can't change the answer
  and it can't touch the repo — put the code it needs in `context`. Selectable in
  `ask_council`/`ask_chain` via the `codex` token (aliased `gpt`) and included in
  the `middle`/`full` council tiers, but not the default council. New env vars
  `ASK_FABLE_CODEX_MODEL`, `ASK_FABLE_CODEX_REASONING` (default `high`), and
  `ASK_FABLE_CODEX_TIMEOUT`.

## [0.7.1] - 2026-07-09

### Added
- Visual **architecture map** (`docs/architecture.html`): a self-contained page
  charting the whole server end to end — the 16 MCP tools grouped by intent, the
  `ask` request pipeline, the six oracle bridges behind `OracleResult`, council
  fan-out vs chain relay, the cross-cutting subsystems, and on-disk state. Linked
  from the README (Install) and the setup guide.

## [0.7.0] - 2026-07-09

A large batch of features accumulated on the `0.6.1` working version since the
last tagged release (`v0.5.1`); this is the release that captures them.

### Added
- Denylist **allowlist**: benign multi-word phrases (e.g. `request payload`) are
  neutralized from a question *before* the offensive-security pattern runs, so an
  ambiguous word like `payload` used in an ordinary engineering sense no longer
  false-trips the guard. Operators can extend the built-in phrases via
  `ASK_FABLE_ALLOWLIST_FILE` (one phrase per line). Only the exact benign phrase
  is rescued — a bare offense term still rejects. (#22)
- **Observability**: a `stats` tool, council/chain audit enrichment, and an
  `AUDIT_RAW` split so the raw question can be kept for debugging while context
  stays hashed-only. (#21)
- **Reliability**: atomic writes, WAL, a session lock, and council/chain
  timeouts. (#19)
- **Chain relay mode** (`ask_chain`): an ordered oracle pipeline with
  anti-anchoring framing. (#17)
- **Reasoning traces**: opt-in inline thinking in results, live-streamed Fable
  reasoning to the console, and persistence of reasoning traces to the answer and
  session dumps. (#14, #16)
- **Council consensus**: a consensus signal with anonymized, Fable-last
  synthesis. (#11)
- **Context bus**: a mini MCP context store with a universal `context_ref`, and
  the sidecar `needs_context` loop wired into the session terminator. (#7, #9, #10)

### Changed
- Consolidated dataclasses and handlers, added circuit-breaking and cache
  hygiene. (#20)
- Dropped the unpublished `salient-core` optional-dependency extra, which broke
  `uv sync` / `uv run` (uv resolves all extras when locking). The guard still
  imports `salient-core` at runtime when it happens to be installed.
- Pinned `mcp>=1.0,<2`.

### Fixed
- `guard.check` measured the **unstripped** question against the max-length cap
  while the min-length check used the stripped value, so trailing whitespace could
  push an otherwise-valid question over the limit. Both checks now use the stripped
  question.
- `_add_thinking` returns early when `ASK_FABLE_THINKING_CHARS <= 0`, disabling the
  reasoning excerpt entirely instead of emitting a bare `" …"`.

[Unreleased]: https://github.com/baggybin/ask-fable/compare/v0.18.0...HEAD
[0.18.0]: https://github.com/baggybin/ask-fable/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/baggybin/ask-fable/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/baggybin/ask-fable/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/baggybin/ask-fable/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/baggybin/ask-fable/compare/v0.13.1...v0.14.0
[0.13.1]: https://github.com/baggybin/ask-fable/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/baggybin/ask-fable/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/baggybin/ask-fable/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/baggybin/ask-fable/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/baggybin/ask-fable/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/baggybin/ask-fable/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/baggybin/ask-fable/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/baggybin/ask-fable/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/baggybin/ask-fable/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/baggybin/ask-fable/compare/v0.5.1...v0.7.0
