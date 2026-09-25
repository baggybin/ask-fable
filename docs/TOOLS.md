# Tool guide

> Part of the [ask-fable README](../README.md).


The server exposes 28 MCP tools. You only need to remember nine entry points:
`ask`, `ask_model`, `list_models`, `ask_council`, `ask_chain`, `ask_debate`,
`ask_verify`, `ask_falsify`, and `ask_conference`. Everything else selects a specific backend,
manages reusable context, or inspects what happened.

> **Quick menu:** [`CLAUDE.md`](../CLAUDE.md) lists all 28 tools grouped by purpose
> (core reasoning · single models · context bus · ops & observability), one line
> each — a fast lookup without the full prose below.

| Goal | Start with | Escalate when |
|---|---|---|
| Solve or debug one problem | `ask` (Fable; `oracle="opus"` for Claude Opus 5 — ~half the price, faster) | use `context_ref` for large reusable context |
| Get one alternate opinion | `ask_model(provider=…)` — `minimax`/`deepseek`/`glm` (cheap direct APIs first), `gemini`/`codex`/`grok`/`kimi` (local CLIs), `ollama`/`atlas`/`ali`/`openrouter` (gateways, pass `model`), `lmstudio` (your LAN LM Studio) | use a council when you need comparison |
| Pick an Atlas model for a task | `list_models(provider="atlas", task="…")` | call `ask_model(provider="atlas", …)` with the accepted selection or rendered picker |
| Cross-check with a second strong model | `ask_council(models=["twin"])` — Fable + Opus 5 on one OAuth session, no keys | add a third voice with `models=["twin","m3"]` |
| Compare several views | `ask_council` | use `ask_chain` when order matters |
| Cross-check Atlas models, GPT adjudicating | `ask_council(provider="atlas")` | pin the panel with `configure_council(provider="atlas")` |
| Make a contentious decision | `ask_debate` | keep the scope narrow; it is the most expensive mode |
| Check a draft answer you already have | `ask_verify` | pass the draft as `answer` and its source material as `context`; only objections code can check count |
| Grind a claim down to what survives evidence | `ask_falsify` | put the corpus the claims must cite in `context`; reuse the same `session` to compound |
| Brainstorm an open question | `ask_conference` | raise `rounds` (default 3, up to 10) when a dilemma needs more back-and-forth |
| Inspect what happened | `trace_list` then `trace_get` | enable full mode only when redacted content is needed |

### The six reasoning functions

| Function | Mental model | Runs | Returns |
|---|---|---:|---|
| `ask` | One expert with memory | One Fable call (or Opus via `oracle="opus"`) | Answer, sidecar, follow-up hints |
| `ask_council` | Independent panel, then synthesis | Parallel + synthesis | Merged answer, sources, consensus |
| `ask_chain` | Draft → critique → decision | Sequential | Final answer, stages, recommendation drift |
| `ask_debate` | Claim → challenge → ruling | Sequential, adversarial | Ruling, claim ledger, resolution |
| `ask_verify` | One reviewer over a supplied draft | Single-pass, annotate-only | Classified objections (prevented / unbacked / self-quoting) |
| `ask_falsify` | Assert → attack → code clerk, persisted | Sequential, adversarial, stateful | Ledger split (survived/killed/open/crucible), reputation |
| `ask_conference` | Brainstorm, models arguing to divergence | Sequential rounds (round 1 blind) | Transcript, map of the disagreement |

Start with `ask`. Choose a council when independence matters, a chain when order
matters, a debate when the disagreement itself needs testing, a falsify run when
claims must bring receipts, and a conference to brainstorm an open question with
several models hearing and building on each other.

<details>
<summary><strong>Complete function reference</strong> — parameters, providers, fallbacks, and response details</summary>

The sections below are the exhaustive reference. For a guided walkthrough with
copyable examples, use the [setup and usage guide](GUIDE.md).

### Single-model reasoning

- **`ask(question, context="", context_ref=None, session="default", reset=false, oracle="fable")`** —
  guarded, **multi-turn** reasoning. `oracle="fable"` (default) keeps the newest Fable;
  `oracle="opus"` switches to **Claude Opus** (newest; `opus5`/`opus55`/`opus48` name
  the same Opus session) — same arguments and result shape, same Claude Code OAuth
  session, roughly **half Fable's price and faster**, so prefer it for high-volume or
  long back-and-forth work and keep Fable for the hardest calls. Reuse the same
  `session` key for follow-ups (the model keeps context server-side); a new key or
  `reset=true` starts a fresh topic. Fable and Opus sessions are namespaced
  separately, so the same key on each is two independent conversations
  (`reset_session(session, model="opus5")` clears the Opus one). Pass **`context_ref`**
  (a key or list of keys stored with `context(op="write", …)`) to pull big context in
  by reference instead of re-pasting. The result carries a `sidecar`; when the model
  wants more it returns a `followup` telling you what to paste and to re-ask on the
  same session (with `likely_already_pasted` flagging what's probably already there).
  Opus is also the **`opus`** token in every multi-model mode: council member or
  `synthesizer`, chain stage, debate proposer/opponent/`adjudicator`.
- **`ask_model(provider, model=None, question, context="", context_ref=None, trusted=false, effort=None)`** —
  one model, single-turn, guarded. `provider` selects the backend; `model` overrides
  it where accepted. Direct APIs (fixed model — a `model` arg is rejected):
  `minimax` (MiniMax-M3; alias `m3`), `deepseek` (deepseek-v4-pro), `glm` (GLM-5.2;
  needs `ASK_FABLE_GLM_API_KEY`, else served by Atlas-hosted GLM on the Atlas key) —
  cheap, prefer these — and `sonnet` (Claude Sonnet 5, same OAuth session as `ask`).
  Local CLIs: `gemini` (Gemini 3.1 Pro via `agy`), `codex` (GPT-5.6 Sol via `codex`;
  alias `gpt`), `grok` (grok-4.6; alias `xai`), `kimi` (kimi-code/k3). A CLI reports
  `{"status":"error","kind":"binary_missing",...}` when absent, and is sandboxed to
  text reasoning (no tools, no filesystem — put the code in `context`). A direct API
  with no key reports `{"status":"error","kind":"not_configured",...}`. Result:
  `{"status":"ok","model":"...","answer":...}`. The former per-backend tools
  (`ask_m3`, `ask_glm`, `ask_deepseek`, `ask_gemini`, `ask_codex`, `ask_grok`,
  `ask_kimi`, `ask_sonnet`) remain callable as unadvertised aliases.
- **`ask_websearch(question, context="", model=None)`** — **opt-in** web-search /
  OSINT research agent, and the **only** `ask_*` tool that browses: it runs a
  search-capable model with **live web search on** and returns a sourced, cited
  answer (findings summary + a `Sources:` list). Pick the backend with `model`:
  `grok` (grok-4.6 live search, the default), a Claude model on the OAuth
  session — `sonnet` / `opus48` / `opus5` / `fable` — using native
  `WebSearch`/`WebFetch`, or `gemini` via the local `agy` CLI.
  All are flat-plan sources (no per-token cost). **Disabled
  by default:** the operator must set `ASK_FABLE_ALLOW_WEBSEARCH=1` (env or config),
  or the call returns `{"status":"disabled",...}`. On `grok` and Claude only
  `WebSearch`/`WebFetch` are re-enabled — Bash/Read/Write and the rest stay blocked.
  `gemini` is **search-only**: `agy` has no tool allow/deny flag of its own, and its
  headless permission policy already denies page fetch, shell and file tools —
  aborting the whole turn (empty stdout, rc 0) when one is attempted. So that
  backend is handed an explicit "`search_web` ONLY" research prompt. Use `grok` or
  Claude when the task needs page content. The Claude seats search through the
  Claude Code SDK's tools, or — when `ASK_FABLE_FABLE_TRANSPORT=http` — through the
  Messages API's own **server-side** web-search tool, so they work on a host with no
  Claude Code; either way the answer comes back with its cited `sources` and the
  billed search count. Results are **not cached**
  (web facts are time-sensitive). Needs the `grok` or `agy` CLI (for those models)
  or the Claude OAuth session (for the Claude models). Use
  `ASK_FABLE_WEBSEARCH_MODEL` /
  `ASK_FABLE_WEBSEARCH_MAX_TURNS` to tune the default backend and search budget.
- **`ask_model(provider="ollama"|"lmstudio"|"atlas"|"openrouter"|"ali", model=None, …)`** —
  the gateway (and CLI-override) providers, where `model` is meaningful. `ollama`
  is an Ollama Cloud model (reached via a local signed-in daemon by default, no
  key). `lmstudio` is a model on the operator's **LAN LM Studio server** — it loads
  a missing model explicitly with a real context window and never bumps a resident
  one off; a load that does not fit returns an `unload_offer` (call
  `list_models(provider="lmstudio")` first). `atlas` is an **Atlas Cloud** text
  model (e.g. `xai/grok-4.6`, `openai/gpt-5.6-sol`); supports `quick`/`standard`/
  `deep` effort, needs `ASK_FABLE_ATLAS_API_KEY`/`ATLASCLOUD_API_KEY`, and its
  gateway 504s a request still generating at ~242s (`atlas_max_tokens`/
  `atlas_timeout` override the preset); `xai/grok-*` routes to the local `grok`
  CLI. `openrouter` is any of **~400 OpenRouter models** behind one key (the
  catch-all; `effort` is clamped to what the model supports; the result reports
  the call's real dollar cost; no fixed ~242s wall); needs
  `ASK_FABLE_OPENROUTER_API_KEY`/`OPENROUTER_API_KEY`, and Grok/Kimi ids reroute to
  the local CLIs. `ali` is an Alibaba/Qwen reasoning model over the token-plan MaaS
  gateway (needs `ASK_FABLE_ALI_API_KEY`). Prefer a local `grok`/`kimi` CLI over an
  equivalent atlas/openrouter id when installed.
- **`list_models(provider, refresh=true, task="", limit=5, interactive=true, all=false)`** —
  one catalogue. `provider` is `ali` (Alibaba/Qwen reasoning models; `all=true` also
  lists the audio/image models), `atlas` or `openrouter` (free live catalogue; with
  `task="…"` a provider-diverse shortlist is ranked from the catalogue's own data —
  reasoning support, context, price, release date — and a native model + effort
  picker opens on clients that support form elicitation, an accepted choice coming
  back as `selection: {action:"accept", model, effort}`; `limit` is 2–8), `ollama`
  (cloud catalogue + locally-pulled models + the configured council), or `lmstudio`
  (loaded/available models, context windows, and a VRAM fit classification).
  Read-only; the catalogues need no key.
- **`ask_council(provider="openrouter", models=[], synthesizer=None)`** — a cross-lab
  panel on one key, with the same GPT-first adjudicator ladder as
  `provider="atlas"`. **`configure_council(provider="openrouter")`** persists your panel.

  You can simply ask your agent: *“Give me the best Atlas models for debugging a
  large Rust repository.”* It should call
  `list_models(provider="atlas", task="debugging a large Rust repository")`, show
  the picker,
  and pass the accepted model and effort to `ask_model(provider="atlas", …)`.

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
  `list_models(provider="atlas", task="review a risky database migration")` first,
  then use
  returned IDs such as
  `models=["fable","atlas:deepseek-ai/deepseek-v4-pro","atlas:zai-org/glm-5.2"]`.
  One `models` entry can be the group token **`twin`** — the *twin flames* — which
  expands to **both Anthropic reasoners at once, `fable` + `opus`**. Both ride the
  same OAuth session as `ask`, so `models=["twin"]` is a dual
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
- **`ask_verify(question, answer=<required>, context="", context_ref=None, reviewer="opus", drafted_by="")`** — review a draft that already exists. The draft is returned unchanged on every path, including a failed review; v1 never suppresses. An objection counts only when code can check its receipt against `context` (a verbatim quote) or a failed check — quoting the draft proves nothing, since every sentence of a draft is present in that draft. Read `verify.prevented` and `verify.unbacked_objections`; a `self-quoting` verdict means supply real source material and re-run. No objections is NOT a correctness guarantee. With **`ASK_FABLE_ALLOW_RUN=1`** and `bwrap` installed, a `run` receipt carries real Python and is executed in the sandbox — the same flag `ask_falsify` uses, and the only way an objection reaches the strongest `executed` class. Without it (or without `bwrap`) nothing runs and such a receipt is a claim that counts for nothing.
- **`ask_falsify(question, context="", context_ref=None, session=<required>, assertor="minimax", falsifier="opus", rounds=1, metamorph=false)`** —
  the **stateful** cousin of `ask_debate`: a falsification ledger that persists
  across calls. An `assertor` states 1–3 typed, checkable claims; a `falsifier`
  (forced to a **different lab**) attacks them; and a deterministic **code clerk** —
  not a model — commits, kills, or leaves open each claim from receipts it verifies
  mechanically: a `cite` quote's **verbatim** presence in `context`, or a `contra`
  edge to a survived claim. A claim can be heard without a receipt, but it can never
  compound (move reputation, survive to the next round) on one — a fabricated or
  absent quote dies. **`session` is required** and is the ledger's persistence key:
  a killed claim stays dead, and calling again with the same key continues the same
  ledger; a new key starts fresh. `rounds` (1–6) runs that many assert→attack cycles
  in one call. `metamorph=true` adds a stability check on unsupported claims — the
  clerk restates the claim semantics-preservingly and re-asks the assertor cold: a
  claim that flips cannot compound, a stable one earns weak support and is reported
  separately as `stable_unverified` (costs two extra model calls per unsupported
  claim; stability is not truth). The result carries the ledger split
  (`survived` / `killed` / `open` / `crucible` / `stable_unverified`) plus per-model
  reputation. With **`ASK_FABLE_ALLOW_RUN=1`** and `bwrap` installed, a claim can
  also be backed or killed by a `run:` receipt — a model-authored Python snippet
  executed in a locked-down sandbox (no network, no filesystem, stdlib only): exit 0
  supports, a non-zero test kills, and a timeout / limit / missing module is
  *inconclusive*, never a refutation. Off by default; without the flag (or without
  `bwrap`) a `run` receipt is reported **unavailable**, never executed naked. Aliases:
  `m3` = minimax, `gpt` = codex, `opus5` = opus.
- **`ask_model(provider="ollama", model=..., question, context="")`** — guarded
  reasoning from a single **Ollama Cloud** model on its own. `model` is a cloud
  model id (e.g. `kimi-k2.7-code:cloud`, `gpt-oss:120b-cloud`, `deepseek-v3.2:cloud`);
  omit it to use `ASK_FABLE_OLLAMA_MODEL`. Single-turn. Reached via your local
  `ollama` daemon by default (needs `ollama signin`; no API key) — point
  `ASK_FABLE_OLLAMA_BASE_URL` at `https://ollama.com` (+ key) for direct cloud.
- **`ask_model(provider="lmstudio", model=..., question, context="")`** — guarded
  reasoning from one model on the operator's **LM Studio** server (LAN, local
  inference — no cloud key, no per-token cost). `model` is an LM Studio key
  (`qwen/qwen3.6-35b-a3b`, `google/gemma-4-31b-qat`); omit it for the configured
  default or the single resident model. Single-turn. A model that is not loaded is
  loaded **explicitly** (never JIT) with a real context window (`lmstudio_context`,
  default 32768, so a long prompt is not silently truncated by the app-wide 4k
  default), and output is capped at `lmstudio_max_tokens` (8192). The **ask-first**
  policy is the default: a load that does not fit next to the residents returns a
  structured `unload_offer` naming what to free (`lmstudio_swap=auto` restores
  automatic unload/wait/restore). A truncated prompt is flagged `kind="truncated"`
  and never cached. `lmstudio:<model>` also works as a token in `ask_chain` /
  `ask_council`.
- **`ask_council(question, context="", models=[...], synthesizer=..., provider=...)`** —
  two ways to scope the panel. With `provider="ollama"|"atlas"|"openrouter"|"lmstudio"`
  the panel is that gateway's: default members come from its configured set (for
  atlas/openrouter, else **3 featured catalog models**), each id gets the right
  prefix, and the adjudicator follows that provider's ladder — GPT-first for
  atlas/openrouter (the **local `codex` CLI** when installed → Atlas/OpenRouter-hosted
  `openai/gpt-5.6-sol` → Fable), Fable otherwise. `provider="lmstudio"` runs the
  panel **one model at a time** (a single GPU serves one at a time; each member we
  load is freed before the next, so a panel larger than VRAM still completes and the
  box is left as found), with the default panel `lmstudio_council` /
  `ASK_FABLE_LMSTUDIO_COUNCIL` (`qwen/qwen3.6-35b-a3b`, `qwen/qwen3.8-27b`,
  `qwen/qwen3.5-9b`, `zai-org/glm-4.6v-flash`, `google/gemma-4-31b-qat`).
  `provider="ollama"` defaults to the configured Ollama set
  (`configure_council` / `ASK_FABLE_OLLAMA_COUNCIL`; kept lean — the 675b/397b
  generalists are left out so the council stays fast, add them per call). An
  explicit `models` is honored within the chosen provider, and `tier` is ignored
  when `provider` is set. The result's `synthesis` block reports which adjudicator
  actually ran (and any fallback).

### Setup and reusable context

- **`list_models(provider="ollama", refresh=true)`** — discover what's actually
  available for the council: the **live `ollama.com` catalog** (GLM, MiniMax-M3,
  Qwen, Kimi, DeepSeek, Nemotron, Mistral, gpt-oss, …) as daemon-ready ids, the
  models already **pulled locally**, and the **currently-configured council**.
  Read-only.
- **`list_models(provider="lmstudio", refresh=true)`** — the LM Studio equivalent:
  loaded models (with the context window they are loaded at, `size_bytes`, and the
  total `loaded_bytes`), the default model, the load-time context/`output_cap` and
  swap policy, plus a **VRAM classification of every loadable model** (`fits_now`,
  `needs_unload`, `too_large`) from the control page. Read-only. Use it to offer a
  concrete choice and to never offer a model that cannot fit at all.
- **`unload_lms_model(model)`** — free one resident LM Studio model. **Operator
  action**: never call it without an explicit request or confirmation — it
  discards a resident model. Refuses while that model is answering an
  `ask_model(provider="lmstudio")` call; waits for the unload to be confirmed and
  reports the freed bytes and what remains resident. Idempotent. A blocked call
  carries an `unload_offer` naming exactly what is in the way.
- **`host_status()`** — read-only GPU/host snapshot from the operator's control
  panel (`GET /api/state.json`, `ASK_FABLE_CONTROL_URL` / config `control_page`):
  GPU utilization, VRAM used/total/free, temperature, fan and power, the processes
  holding VRAM, service states, the true loaded LM Studio set, and warnings (e.g.
  a LiteLLM default key). The same reading powers the `ask_model(provider="lmstudio")`
  room check; unreachable returns a clean error, never a crash.
- **`configure_council(provider, models=[...], synthesizer=..., default_model=...)`** —
  **save** a provider council default so it sticks across sessions. `provider` is
  `"ollama"`, `"atlas"`, or `"openrouter"`. Writes ask_fable's config file
  (`${XDG_CONFIG_HOME:-~/.config}/ask_fable/config.json`), which **overrides** the
  matching `ASK_FABLE_*_COUNCIL` env defaults. `models` is that provider's default
  panel (bare ids or prefixed tokens; Ollama bare names are normalized
  `minimax-m3` → `minimax-m3:cloud`). Atlas/OpenRouter also take `synthesizer` (the
  adjudicator; aliases resolve, `gpt` persists as `codex`); Ollama takes
  `default_model` for `ask_model(provider="ollama")`. Ground the picks with
  `list_models(provider=…)` first, and confirm with the user.
- **`configure_tracing(trace_mode="safe"|"full", stream_reasoning=true|false)`** —
  toggle reasoning-trace capture **at runtime**, persisted to the same config file.
  `trace_mode="full"` records redacted model reasoning into traces / trace bundles
  (and saves answer markdown); `stream_reasoning` streams model thinking live to the
  server console. Both **override** the `ASK_FABLE_TRACE_MODE` /
  `ASK_FABLE_STREAM_REASONING` env defaults and apply on the next call — no
  `~/.claude.json` edit or restart. Pass either or both.
- **`context_read(key="")`** — the **context bus** read half: pass `key` to read a
  stored blob back (value + size/age/description), or omit it to **list** every
  stored key (metadata only, never the full values). Read-only. `context_read()` is
  the way to discover what's already available before re-pasting.
- **`context(op, key, value="", description="", paths=[...], max_chars=...)`** — the
  bus write half, dispatched by `op`. `op="write"` stores a chunk of context (code, a
  stack trace, design notes) under a stable `key`, then reference it via `context_ref`
  on any ask tool instead of re-pasting; `op="pack"` reads the repo files in `paths`
  (`path` or `path:START-END`, relative to the configured project root) into a budgeted
  bundle under `key`; `op="delete"` removes `key`. Shared by every agent on the server
  (a sibling agent can read it); reusing a key overwrites. A durable best-effort
  SQLite store (`${XDG_STATE_HOME}/ask_fable/context.db`, override with
  `ASK_FABLE_CONTEXT_PATH`).
- **`code_index(rebuild=false)`** — build or refresh the local **code+docs index**
  for the configured project root (same root as `context(op="pack")`): walks the tree
  (skipping `.git`, dependency/build dirs, binaries, oversize files and the
  `.env` blocklist), splits files into overlapping line windows, and stores them
  in a per-project SQLite index **outside the repo**
  (`${XDG_STATE_HOME}/ask_fable/code_index/`). Incremental — unchanged files keep
  their embeddings. Embeddings are **opt-in and fail-safe**: hosts come from
  `ASK_FABLE_EMBED_HOSTS` (comma-separated, tried in order; unset = the LM Studio
  host), and when none answers the chunks are stored unembedded and search
  degrades to keyword until a later run backfills.
- **`code_search(query, k=8, rerank=false)`** — hybrid **FTS5 keyword + embedding**
  search over that index, fused by reciprocal rank, returning the best windows as
  `path:start-end` with a snippet. No embed host reachable = keyword-only with a
  `degraded` note instead of a failure. `rerank=true` reorders the top hits with
  the chat model in `ASK_FABLE_EMBED_RERANK_MODEL` (skipped with a note when unset
  or unreachable). Returns `not_indexed` until `code_index` has run for this root.
- **`ask_fable_help(topic="all")`** — the server's manual on demand: free, local,
  instant, no model call. Claude Code (and other harnesses) truncate the MCP
  standing-instructions field at ~2 KB, so that field carries only the triggers
  and the rest lives here — what to do with a refusal, the shared context bus,
  configuring Ollama / Atlas / OpenRouter councils, and the full tool menu with
  every model token. Call it with no argument for everything; every response
  lists the topics it accepts. Guard refusals also carry a `how_to_reframe`
  field pointing back at it, plus `where` (`question` or `context` — the guard
  scans both) naming the field that tripped it. A refusal from the model
  provider's own safeguard carries a distinct `how_to_reframe` (`stage:"model"`).
- **`reset_session(session="default", model="fable", save=true)`** — dump the transcript
  (each turn's Q/A and any provider reasoning captured for that turn) to
  `${XDG_STATE_HOME}/ask_fable/sessions/<key>-<ts>.md` (when `save`) and clear it.
  `model` selects which conversation to clear — `"fable"` for `ask`'s Fable
  threads, or an Opus token (`"opus5"`, `"opus"`) for `ask(oracle="opus")` (they
  namespace sessions separately).

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

For `ask_council` (including its `provider=…` forms), `ask_chain`, and
`ask_debate`, `session` is
a hub coordination key, not a Fable multi-turn
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

1. call **`list_models(provider="ollama")`** — which returns the live `ollama.com`
   catalog, the
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

2. **ask you** which of those you want, then call
   **`configure_council(provider="ollama", …)`** with:

   ```json
   {
     "provider": "ollama",
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
across sessions and every later `ask_council(provider="ollama")` (and the `full`
tier) uses it.
Precedence, highest first: **config file → env var → built-in default**.
