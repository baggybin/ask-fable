# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
  (the deterministic denylist is unchanged and still only scans the question).
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

[Unreleased]: https://github.com/baggybin/ask-fable/compare/v0.12.0...HEAD
[0.12.0]: https://github.com/baggybin/ask-fable/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/baggybin/ask-fable/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/baggybin/ask-fable/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/baggybin/ask-fable/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/baggybin/ask-fable/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/baggybin/ask-fable/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/baggybin/ask-fable/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/baggybin/ask-fable/compare/v0.5.1...v0.7.0
