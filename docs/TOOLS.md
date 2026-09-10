# Tool guide

> Part of the [ask-fable README](../README.md).


The server exposes 39 MCP tools. You only need to remember five entry points:
`ask`, `ask_council`, `ask_chain`, `ask_debate`, and `ask_conference`. Everything
else selects a specific backend, manages reusable context, or inspects what happened.

> **Quick menu:** [`CLAUDE.md`](../CLAUDE.md) lists all 39 tools grouped by purpose
> (core reasoning · single models · context bus · ops & observability), one line
> each — a fast lookup without the full prose below.

| Goal | Start with | Escalate when |
|---|---|---|
| Solve or debug one problem | `ask` (Fable) or `ask_opus5` (Claude Opus 5 — ~half the price, faster) | use `context_ref` for large reusable context |
| Get one alternate opinion | `ask_m3`, `ask_deepseek`, `ask_glm` (cheap direct APIs first), `ask_gemini`, `ask_codex`, `ask_grok`, `ask_kimi`, `ask_ollama`, `ask_atlas`, or `ask_openrouter` (~400 models, one key) | use a council when you need comparison |
| Pick an Atlas model for a task | `list_atlas_models(task="…")` | call `ask_atlas` with the accepted selection or rendered picker |
| Cross-check with a second strong model | `ask_council(models=["twin"])` — Fable + Opus 5 on one OAuth session, no keys | add a third voice with `models=["twin","m3"]` |
| Compare several views | `ask_council` | use `ask_chain` when order matters |
| Cross-check Atlas models, GPT adjudicating | `ask_atlas_council` | pin the panel with `configure_atlas_council` |
| Make a contentious decision | `ask_debate` | keep the scope narrow; it is the most expensive mode |
| Brainstorm an open question | `ask_conference` | raise `rounds` (default 3, up to 10) when a dilemma needs more back-and-forth |
| Inspect what happened | `trace_list` then `trace_get` | enable full mode only when redacted content is needed |

### The five reasoning functions

| Function | Mental model | Runs | Returns |
|---|---|---:|---|
| `ask` / `ask_opus5` | One expert with memory | One Fable / Opus 5 call | Answer, sidecar, follow-up hints |
| `ask_council` | Independent panel, then synthesis | Parallel + synthesis | Merged answer, sources, consensus |
| `ask_chain` | Draft → critique → decision | Sequential | Final answer, stages, recommendation drift |
| `ask_debate` | Claim → challenge → ruling | Sequential, adversarial | Ruling, claim ledger, resolution |
| `ask_conference` | Brainstorm, models arguing to divergence | Sequential rounds (round 1 blind) | Transcript, map of the disagreement |

Start with `ask`. Choose a council when independence matters, a chain when order
matters, a debate when the disagreement itself needs testing, and a conference to
brainstorm an open question with several models hearing and building on each other.

<details>
<summary><strong>Complete function reference</strong> — parameters, providers, fallbacks, and response details</summary>

The sections below are the exhaustive reference. For a guided walkthrough with
copyable examples, use the [setup and usage guide](GUIDE.md).

### Single-model reasoning

- **`ask(question, context="", context_ref=None, session="default", reset=false)`** —
  guarded reasoning from **Fable**. Reuse the same `session` key for follow-ups (Fable
  keeps context server-side); a new key or `reset=true` starts a fresh topic. Pass
  **`context_ref`** (a key or list of keys stored with `context_write`) to pull big
  context in by reference instead of re-pasting. The result carries a `sidecar`; when
  the model wants more it returns a `followup` telling you what to paste and to re-ask
  on the same session (with `likely_already_pasted` flagging what's probably already
  there). All ask tools accept `context` and `context_ref`.
- **`ask_opus5(question, context="", context_ref=None, session="default", reset=false)`** —
  the same tool on **Claude Opus 5 (`claude-opus-5`)**: identical arguments, identical
  result shape, same multi-turn `session`/`reset` model, same Claude Code OAuth session
  (no API key, nothing extra to configure). Opus 5 is roughly **half Fable's price and
  faster**, so prefer it for high-volume or long back-and-forth work and keep `ask` for
  the hardest calls; running both on one question is a cheap two-model cross-check.
  Sessions are namespaced per tool — the same key on `ask` and `ask_opus5` is two
  independent conversations, and `reset_session(session, model="opus5")` clears this
  one. Opus 5 is also the **`opus`** token (aliases `opus5`, `opus-5`) in every
  multi-model mode: council member or `synthesizer`, chain stage, debate
  proposer/opponent/`adjudicator`.
- **`ask_m3(question, context="")`** — the same guarded reasoning from **MiniMax
  (`MiniMax-M3`)** on its own, independent of Fable. Single-turn. Returns
  `{"status":"ok","model":"MiniMax-M3","answer":...}`.
- **`ask_glm(question, context="")`** — the same guarded reasoning from **GLM
  (`glm-5.2`)** on its own, via Z.ai's Anthropic-compatible endpoint. Single-turn.
  Requires `ASK_FABLE_GLM_API_KEY` on the server (returns
  `{"status":"error","kind":"not_configured",...}` otherwise). Returns
  `{"status":"ok","model":"glm-5.2","answer":...}`.
- **`ask_deepseek(question, context="")`** — the same guarded reasoning from
  **DeepSeek (`deepseek-v4-pro`)** on its own, via DeepSeek's Anthropic-compatible
  endpoint. Cheap direct API — prefer it over pricier cloud models for a quick
  independent opinion. Single-turn. Requires `ASK_FABLE_DEEPSEEK_API_KEY` on the
  server (returns `{"status":"error","kind":"not_configured",...}` otherwise).
  Returns `{"status":"ok","model":"deepseek-v4-pro","answer":...}`.
- **`ask_gemini(question, context="")`** — the same guarded reasoning from
  **Google Gemini (`Gemini 3.1 Pro (High)`)** on its own, via the
  already-authenticated local `agy` CLI (no API key set by the server — like
  `mmx`). Single-turn. Requires the `agy` CLI installed and signed in (returns
  `{"status":"error","kind":"binary_missing",...}` otherwise). Returns
  `{"status":"ok","model":"Gemini 3.1 Pro (High)","answer":...}`.
- **`ask_codex(question, context="")`** — the same guarded reasoning from
  **OpenAI (`gpt-5.6-sol`)** on its own, via the already-authenticated local
  `codex` CLI in non-interactive `codex exec` mode (no API key set by the server —
  like `mmx`/`agy`). Runs hermetically and read-only — it can't see your repo, so
  put the code it needs in `context`. Single-turn. Requires the `codex` CLI
  installed and logged in (returns `{"status":"error","kind":"binary_missing",...}`
  otherwise). Returns `{"status":"ok","model":"gpt-5.6-sol","answer":...}`.
- **`ask_grok(question, context="", effort=None)`** — guarded, single-turn
  reasoning from **Grok (`grok-4.6`)** through the already-authenticated local
  `grok` CLI. The default low reasoning effort keeps context-heavy turns bounded;
  override it with `effort` or `ASK_FABLE_GROK_REASONING`. Requires the `grok` CLI
  installed and logged in (returns `{"status":"error","kind":"binary_missing",...}`
  otherwise). Returns `{"status":"ok","model":"grok-4.6","answer":...}`.
- **`ask_kimi(question, context="", effort=None)`** — guarded, single-turn
  reasoning from **Kimi (`kimi-code/k3`)** through the local `kimi` CLI on your
  Kimi Code subscription, sandboxed to pure text reasoning (no tools, no
  filesystem). Prefer it over `ask_atlas` with `moonshotai/kimi-*`: same model
  family, no Atlas key, no per-token billing. The CLI passes the prompt as one
  argv value, which the kernel caps near 131k bytes, so an oversized prompt is
  refused with `{"status":"error","kind":"context_too_large",...}` pointing at
  the HTTP route. Returns `{"status":"ok","model":"kimi-code/k3","answer":...}`.
- **`ask_atlas(question, context="", model=None, effort=None)`** — guarded,
  single-turn reasoning from an **Atlas Cloud** text model (for example
  `xai/grok-4.6` or `openai/gpt-5.6-sol`) over the OpenAI
  `/v1/chat/completions` shape. Supports `quick`, `standard`, and `deep`
  effort. It needs `ASK_FABLE_ATLAS_API_KEY` or `ATLASCLOUD_API_KEY`, except
  `xai/grok-*` models route to the authenticated local `grok` CLI when present.
  Returns `{"status":"ok","model":"...","answer":...}`.
- **`ask_openrouter(question, context="", model=None, effort=None)`** — guarded,
  single-turn reasoning from any of **~400 OpenRouter models** (Anthropic, OpenAI,
  Google, DeepSeek, Meta, Qwen, Moonshot, xAI, Mistral, …) behind **one API key**.
  The catch-all for a model with no dedicated tool, and the cheapest way to compare
  labs without configuring each provider. Call `list_openrouter_models` first — the
  catalog is free. Needs `ASK_FABLE_OPENROUTER_API_KEY` (or `OPENROUTER_API_KEY`);
  Grok and Kimi ids reroute to the local `grok`/`kimi` CLIs when installed.
  Unlike Atlas, `effort` is clamped to what the chosen model actually supports —
  OpenRouter publishes each model's reasoning efforts, so there is no wasted probe
  request. The result reports the call's **real dollar cost**.
- **`list_openrouter_models(refresh=true, task="", limit=5, interactive=true)`** —
  the live OpenRouter catalog with price per million, context window and per-model
  reasoning support. Free (no key). `task="…"` ranks a provider-diverse shortlist
  from the catalog's own fields — reasoning support, context, price, release date —
  so a model released today ranks correctly with no change here. A task mentioning
  cheap/fast/high-volume flips the ranking toward the cheap and free tiers.
- **`ask_openrouter_council(question, models=[], synthesizer=None)`** — a cross-lab
  panel on one key, with the same GPT-first adjudicator ladder as
  `ask_atlas_council`. **`configure_openrouter_council`** persists your panel.
- **`list_atlas_models(refresh=true, task="", limit=5, interactive=true)`** —
  fetch the free live Atlas text-model catalog. With `task`, it ranks a
  provider-diverse shortlist from the catalog's capability profiles, tags,
  context window, latency, and pricing. On MCP clients that support form
  elicitation it opens a native model + effort picker; otherwise it returns the
  same choices under `picker` for the host to render. An accepted native choice
  is returned as `selection: {action:"accept", model, effort}`. `limit` is 2–8
  (default 5); `refresh:false` makes no network call and returns only effort
  choices. The ranking is live metadata-based guidance, not an independent
  benchmark.

  You can simply ask your agent: *“Give me the best Atlas models for debugging a
  large Rust repository.”* It should call
  `list_atlas_models(task="debugging a large Rust repository")`, show the picker,
  and pass the accepted model and effort to `ask_atlas`.

<p align="center">
  <img src="../images/ask_atlas_new.jpg" alt="Abstract high-tech visualization of an atlas, mapping out AI reasoning models in a cloud network">
</p>

### Multi-model reasoning

- **`ask_council(question, context="", models=["fable","minimax"])`** — ask
  **several models the same question in parallel**, then have Fable **synthesize**
  their answers into one merged answer (reconciling conflicts on the merits). The
  payload also returns each oracle's raw answer under `sources`. Single-turn.
  Degrades to whichever oracle(s) answered, and only refuses/errors when none do.
  `models` picks from **`fable`** (the newest Fable), **`fable51`** (claude-fable-5-1,
  pinned), **`opus`** (claude-opus-5, same
  OAuth session as Fable — always available), **`minimax`** (MiniMax-M3, via
  the `mmx` CLI), **`gemini`** (Gemini 3.1 Pro, via the `agy` CLI), **`codex`**
  (GPT-5.6 Sol, via the `codex` CLI), **`glm`** (GLM-5.2, via Z.ai's Anthropic
  endpoint), and **`deepseek`** (deepseek-v4-pro, via DeepSeek's Anthropic
  endpoint) — e.g.
  `models=["fable","opus","minimax","gemini","codex","glm","deepseek"]` for a
  seven-model council.
  `gemini` needs the local `agy` CLI; `glm` and `deepseek` require API keys
  configured on the server (below); an
  unconfigured or unreachable oracle is reported in `sources` and skipped, never
  fatal. No provider keys are set by the server itself — each bridge reuses env
  config or an already-authenticated CLI/session. You can also add **Ollama Cloud**
  models as `ollama:<model>` tokens — e.g.
  `models=["fable","ollama:qwen3-coder:480b-cloud","ollama:nemotron-3-ultra:cloud"]`
  (reached via your local `ollama` daemon by default — no key). Atlas Cloud
  models work the same way with `atlas:<model-id>` tokens in councils, chains,
  and debates. For a task-matched multi-model call, call
  `list_atlas_models(task="review a risky database migration")` first, then use
  returned IDs such as
  `models=["fable","atlas:deepseek-ai/deepseek-v4-pro","atlas:zai-org/glm-5.2"]`.
  One `models` entry can be the group token **`twin`** — the *twin flames* — which
  expands to **both Anthropic reasoners at once, `fable` + `opus`**. Both ride the
  same OAuth session as `ask`/`ask_opus5`, so `models=["twin"]` is a dual
  Fable/Opus 5 invocation that needs no provider keys at all — the cheapest real
  second opinion available — and `models=["twin","minimax"]` adds a third voice to
  it. `twins`, `twin flames`, `twin-flame` and `twin_flames` all name the same
  pair. A group only makes sense where a *list* of models is taken; a
  single-model slot (`synthesizer`, and the debate roles) rejects it with a
  `bad_args` error rather than silently keeping just Fable.
  Instead of listing `models`, pass a named
  **`tier`**: `"default"` (fable+minimax, +deepseek when `ASK_FABLE_DEEPSEEK_API_KEY`
  is set — cheap direct models are preferred and consulted first) ·
  `"twin"` (the twin flames, fable+opus) ·
  `"middle"` (+opus+glm+gemini+codex+grok+kimi, cheap-first order) · `"full"`
  (+the configured Ollama Cloud models). An explicit `models` list overrides `tier`.
  The result adds a **`consensus`** signal (`strong`/`partial`/`divergent`/`unknown`) +
  `material_disagreement` computed from the panel's recommendations, each `sources`
  entry shows that model's `recommendation`, and the synthesizer sees the panel
  **anonymized** (Expert A/B, Fable last) so it can't favor its own answer — on a
  material split it's told to pick a side, not average.
- **`ask_chain(question, context="", pipeline="m3 > glm > deepseek > fable")`** — the
  **sequential** counterpart to `ask_council`: thread a question through an **ordered**
  pipeline (a `pipeline` string split on `>`, or an ordered `models` array), each stage
  refining the last. Stage 1 **drafts**; each middle stage is told to solve
  independently and **critique** the prior draft before extending it (an anti-anchoring
  guard); the final stage **decides**, seeing all prior stages anonymized as peers. Order
  matters and repeats are allowed (`fable > glm > fable` = draft → critique → re-decide;
  alias `m3` = minimax). The `twin` group token expands **in place to two stages**,
  `fable` then `opus` — so `m3 > twin` is a cheap draft finished by both Anthropic
  reasoners in turn. A stage that refuses/errors is **skipped** (recorded) and the
  chain continues; if the final stage fails, Fable synthesizes the survivors. The result
  adds a **`recommendation_drift`** trail + **`material_drift`** flag — the chain analogue
  of the council's consensus signal, so you can see whether the answer was refined or just
  rubber-stamped. Best for **cost-tiered escalation** (a cheap/fast model drafts, Fable
  finalizes) and explicit **draft → red-team → decide** pipelines; costs more latency than
  a council (stages run in sequence, not parallel), so reserve it for when the ordered
  refinement is the point.
- **`ask_debate(question, context="", proposer="fable", opponent="minimax", adjudicator="fable", rounds=1)`** —
  the **adversarial** counterpart: pit two models AGAINST each other, then have a fresh
  anonymized third model adjudicate. The `proposer` commits to a position decomposed into
  load-bearing **claims**; the `opponent` must **dispose of each claim** (concede, or
  contest with a concrete failure scenario); the proposer **revises** under fire; the
  `adjudicator` **rules** on the merits. Pick the pair (e.g. `opponent="codex"` for
  **Fable vs GPT-5.6 Sol**, or `opponent="glm"`) and, if you want someone other than
  Fable ruling, the judge (`adjudicator="opus"` for Claude Opus 5) — keep it off the
  debating pair so the ruling stays third-party. `rounds=2` adds a rebuttal pass. The
  outcome is decided **server-side** from the ledger, surfaced as `debate.resolution`:
  **`conceded`** (opponent conceded everything), **`converged`** (all contests resolved
  and both sides agree), **`adjudicated`** (the adjudicator decided), or **`stalemate`**
  (both dug in with nothing new → confidence is mechanically downgraded). Also returns
  `recommendation_drift`, `decisive_argument`, and `low_effort_opposition`. Degrades to a
  single-critic pass if the opponent is unconfigured. The **most expensive** mode (up to
  four sequential calls), so reserve it for a genuinely contentious, hard-to-reverse
  decision. Aliases: `m3` = minimax, `gpt` = codex, `opus5` = opus.
- **`ask_ollama(question, context="", model=...)`** — guarded reasoning from a
  single **Ollama Cloud** model on its own. `model` is a cloud model id (e.g.
  `kimi-k2.7-code:cloud`, `gpt-oss:120b-cloud`, `deepseek-v3.2:cloud`); omit it to
  use `ASK_FABLE_OLLAMA_MODEL`. Single-turn. Reached via your local `ollama`
  daemon by default (needs `ollama signin`; no API key) — point
  `ASK_FABLE_OLLAMA_BASE_URL` at `https://ollama.com` (+ key) for direct cloud.
- **`ask_ollama_council(question, context="", models=[...])`** — fan a question
  out to **several Ollama Cloud models** (an `ollama:` prefix on each id is
  optional), then have Fable synthesize their answers into one — same contract as
  `ask_council`, but the council is Ollama-only. Omit `models` to use the server's
  configured set (the config file or `ASK_FABLE_OLLAMA_COUNCIL`, default:
  `minimax-m3:cloud`, `glm-5.2:cloud`, `nemotron-3-ultra:cloud`,
  `qwen3-coder:480b-cloud`, `kimi-k2.7-code:cloud`, `deepseek-v4-pro:cloud`,
  `gpt-oss:120b-cloud` — kept lean; the 675b/397b generalists are left out so the
  parallel council stays fast, add them per call if you want them).
- **`ask_atlas_council(question, context="", models=[...], synthesizer=...)`** —
  the **Atlas-only council with GPT-5.6 Sol as the default adjudicator**. Fans the
  question out to several Atlas Cloud models (an `atlas:` prefix on each id is
  optional), then the adjudicator reconciles them: the **local `codex` CLI**
  (GPT-5.6 Sol, no Atlas tokens) when installed → Atlas-hosted
  `openai/gpt-5.6-sol` → Fable. Omit `models` to use the configured set
  (`configure_atlas_council` / `ASK_FABLE_ATLAS_COUNCIL`), else **3 featured
  catalog models** (one per provider). `xai/grok-*` members reroute to the local
  `grok` CLI keylessly; anything else needs the Atlas API key. The result's
  `synthesis` block reports which adjudicator actually ran (and any fallback).
  The same `synthesizer` parameter also works on plain `ask_council`.

### Setup and reusable context

- **`list_ollama_models(refresh=true)`** — discover what's actually available for
  the council: the **live `ollama.com` catalog** (GLM, MiniMax-M3, Qwen, Kimi,
  DeepSeek, Nemotron, Mistral, gpt-oss, …) as daemon-ready ids, the models already
  **pulled locally**, and the **currently-configured council**. Read-only.
- **`configure_ollama_council(models=[...], default_model=...)`** — **save** a
  chosen Ollama council so it sticks across sessions. Writes ask_fable's config
  file (`${XDG_CONFIG_HOME:-~/.config}/ask_fable/config.json`), which **overrides**
  the `ASK_FABLE_OLLAMA_*` env defaults. Bare names are normalized (`minimax-m3` →
  `minimax-m3:cloud`); an `ollama:` prefix is optional. Together these two tools
  let an agent, the first time you want an Ollama council, **offer to set it up** —
  list the options, ask which you want, and persist your pick — instead of you
  hand-editing env vars.
- **`configure_atlas_council(models=[...], synthesizer=...)`** — **save** a chosen
  Atlas council (and optionally its adjudicator) so it sticks across sessions.
  Writes the same config file (`atlas_council` / `atlas_synthesizer` keys), which
  **overrides** the `ASK_FABLE_ATLAS_COUNCIL` / `ASK_FABLE_ATLAS_SYNTHESIZER` env
  defaults. An `atlas:` prefix is optional; aliases resolve (`gpt` persists as
  `codex`, a bare `openai/gpt-5.6-sol` as `atlas:openai/gpt-5.6-sol`). Ground the
  picks with `list_atlas_models` first.
- **`configure_tracing(trace_mode="safe"|"full", stream_reasoning=true|false)`** —
  toggle reasoning-trace capture **at runtime**, persisted to the same config file.
  `trace_mode="full"` records redacted model reasoning into traces / trace bundles
  (and saves answer markdown); `stream_reasoning` streams model thinking live to the
  server console. Both **override** the `ASK_FABLE_TRACE_MODE` /
  `ASK_FABLE_STREAM_REASONING` env defaults and apply on the next call — no
  `~/.claude.json` edit or restart. Pass either or both.
- **`context_write(key, value, description="")`** — the **context bus**: store a
  chunk of context (code, a stack trace, design notes) under a stable `key`, then
  reference it via `context_ref` on any ask tool instead of re-pasting. Shared by
  every agent on the server (a sibling agent can read it); reusing a key overwrites.
  A durable best-effort SQLite store (`${XDG_STATE_HOME}/ask_fable/context.db`,
  override with `ASK_FABLE_CONTEXT_PATH`).
- **`context_read(key)`** / **`context_list()`** / **`context_delete(key)`** —
  read back a stored blob (+ size/age/description), list what's stored (keys +
  metadata, never the full values), or delete one. `context_list` is the way to
  discover what's already available before re-pasting.
- **`ask_fable_help(topic="all")`** — the server's manual on demand: free, local,
  instant, no model call. Claude Code (and other harnesses) truncate the MCP
  standing-instructions field at ~2 KB, so that field carries only the triggers
  and the rest lives here — what to do with a refusal, the shared context bus,
  configuring Ollama / Atlas / OpenRouter councils, and the full tool menu with
  every model token. Call it with no argument for everything; every response
  lists the topics it accepts. Guard refusals also carry a `how_to_reframe`
  field pointing back at it.
- **`reset_session(session="default", model="fable", save=true)`** — dump the transcript
  (each turn's Q/A and any provider reasoning captured for that turn) to
  `${XDG_STATE_HOME}/ask_fable/sessions/<key>-<ts>.md` (when `save`) and clear it.
  `model` selects which tool's conversation to clear — `"fable"` for `ask`, `"opus5"`
  for `ask_opus5` (they namespace sessions separately).

### Operations and observability

- **`stats(window="24h", by="model", model=..., session=...)`** — read-only
  usage/health stats aggregated from the audit log (rotations included): per-bucket
  calls / allowed / refused / errors, avg + p95 latency, and error rate, plus totals.
  `window` is `1h`/`24h`/`7d`/`all`; `by` buckets per `model` (a council counts under
  its synthesizer), `provider` (per backend call — the only view that sees
  council/chain/debate members one by one), `tool`, `session`, `day`, `project`,
  `cache`, or `mode`; the optional filters narrow to one backend or workflow. Calls
  the circuit breaker shed are reported as `circuit_open`, not as errors or
  latency. Council/chain audit records
  also carry `quorum`/`consensus`/`synth_fallback`, so you can see degradation trends
  ("have my councils been running 1-of-5 all day?") — no model call, never cached.
- **`trace_list(limit=20, tool=..., status=..., provider=..., session=..., project=..., before=...)`** —
  list recent schema-v2 request traces without raw content. Filter by request or
  provider metadata and page with `before`.
- **`trace_get(trace_id, include_content=false, max_chars=4000)`** — read one ordered
  event timeline and its artifact references. In full mode, `include_content=true`
  returns a redacted, bounded excerpt of that trace's bundle.

### Cross-instance session hub

The hub is a **local coordination dashboard**, not a second form of model memory.
Every successful ask that passes the tool-level cache is mirrored *after* its
oracle result is available. That result can still come from an underlying
per-oracle cache.
Use a meaningful shared `session` label when several local agents are working the
same decision, then inspect that work without re-running it:

```jsonc
ask({ "question": "Which migration path is safest?", "context": "…", "session": "db-migration" })

session_list({})                              // live sessions in this project
session_peek({ "session_key": "db-migration" }) // retained Q/A turns across agents
session_stats({ "window_s": 86400 })          // 24-hour totals by oracle, agent, status
```

- **`session_list(all_projects=false, active_only=true, limit=50)`** lists one
  row per `(session_key, agent_id)`, newest first. It is scoped to the current
  project by default; `all_projects:true` exposes every project recorded by this
  local database. `active_only:true` hides sessions whose heartbeat is older than
  five minutes (tune with `ASK_FABLE_HUB_STALE_SECONDS`); pass `false` to include
  retained history. `limit` is 1–200.
- **`session_peek(session_key, agent_id=None)`** returns the full retained question
  and answer history in chronological order. It intentionally spans projects for
  a matching session label; pass `agent_id` to narrow it. Choose labels that do
  not collide across sensitive work, and do not use this tool where you are not
  permitted to read the local user’s other project data.
- **`session_stats(all_projects=false, window_s=86400)`** aggregates turns by
  oracle, agent, and status across MCP instances. It is project-scoped by default;
  `window_s:0` includes all retained turn history. Its session totals are not
  restricted to that time window.

The hub is deliberately **visibility-only**: it is never read by `ask`, councils,
chains, or debates; it cannot resume Fable's per-process `SessionStore`; and
refused/error turns are not mirrored. It therefore cannot feed another agent’s
history back into an oracle answer automatically. An agent can still explicitly
read a hub turn and relay it in a later prompt. It retains complete questions and
answers, plus session, agent, project, oracle, status, timing, and an SDK session
identifier when supplied. It does not separately store the supplied `context`,
although a response can echo it. Treat its database as sensitive. The default path is
`${XDG_STATE_HOME:-~/.local/state}/ask_fable/hub.db` (new files are owner-only
`0600`, SQLite WAL, per-operation connections). It is machine-local unless you
deliberately set `ASK_FABLE_HUB_PATH` to shared storage.

For `ask_council`, `ask_chain`, `ask_debate`, `ask_ollama_council`, and
`ask_atlas_council`, `session` is a hub coordination key, not a Fable multi-turn
session; if omitted it defaults to the tool name. Hub retention is a best-effort row cap, not a deletion schedule:
the default 10,000 stored turns are swept oldest-first roughly every 100 writes.
Disabling the hub stops future reads and writes but does not delete already stored
data.

</details>

### Configure the Ollama council

You never have to hand-edit env vars to choose your Ollama council — the agent can
set it up for you. The **first time** you want an Ollama council (or any time you
say *"configure ask_fable"* / *"set up the council"*), the server's instructions
prompt the agent to:

1. call **`list_ollama_models`** — which returns the live `ollama.com` catalog, the
   models already pulled locally, and the currently-configured council:

   ```json
   { "status": "ok", "reachable": true,
     "available_cloud": ["deepseek-v4-pro:cloud", "glm-5.2:cloud", "minimax-m3:cloud",
                          "mistral-large-3:675b-cloud", "nemotron-3-ultra:cloud",
                          "qwen3-coder:480b-cloud", "..."],
     "pulled_local": ["gpt-oss:120b-cloud"],
     "configured_council": ["minimax-m3:cloud", "glm-5.2:cloud", "..."],
     "config_file": "~/.config/ask_fable/config.json" }
   ```

2. **ask you** which of those you want, then call **`configure_ollama_council`** with:

   ```json
   {
     "models": [
       "minimax-m3",
       "glm-5.2",
       "qwen3-coder:480b-cloud",
       "deepseek-v4-pro"
     ],
     "default_model": "gpt-oss:120b-cloud"
   }
   ```

The choice is written to `${XDG_CONFIG_HOME:-~/.config}/ask_fable/config.json`
(`{"ollama_council": [...], "ollama_model": "..."}`) and **overrides** the
`ASK_FABLE_OLLAMA_COUNCIL` / `ASK_FABLE_OLLAMA_MODEL` env vars — so it persists
across sessions and every later `ask_ollama_council` (and the `full` tier) uses it.
Precedence, highest first: **config file → env var → built-in default**.
