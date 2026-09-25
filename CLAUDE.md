# CLAUDE.md

Guidance for working in this repo.

## ask_fable tool menu

The `ask_fable` MCP server exposes strong external reasoning models as a second
brain for software/engineering work. Reach for `ask` liberally and early — before
guessing at unfamiliar code, when weighing a design trade-off, or to sanity-check
a plan or diff. The models can't see the repo, so put the real code in `context`
(or point at files with `context(op="pack", …)`). Scope is software/engineering, including
conceptual/brainstorming questions (ideation, ideas for future code — no context
needed). Refused only when the question itself directly asks for
offensive-security work (exploit development, attack tooling) or non-software
domain knowledge (e.g. biology); questions about security-related code are fine.

When a tool would clearly help but you don't call it yourself, tell the operator in
one line — which tool and why — so they can opt in; don't silently skip it.

> A local copy of this menu prints on demand via
> `python3 ~/.claude/ask_fable_menu.py`.

### Core reasoning

| Tool | What it does |
|------|--------------|
| `ask` | Default move. **Multi-turn**; ask Fable about your code/design. Put code in `context`; reuse `session` for follow-ups. Add `oracle="opus"` for Claude Opus (newest; `opus5`/`opus55`/`opus48` name the same session) — same args/results, same OAuth session, ~half Fable's price and faster; Opus sessions are namespaced separately. `ASK_FABLE_FABLE_MODEL` / `ASK_FABLE_OPUS_MODEL` pin exact ids. |
| `ask_fable_help` | The server's manual, on demand — free, local, instant, never truncated. Harnesses cut the MCP standing-instructions field at ~2 KB, so the detail lives here: what to do with a refusal, the shared context bus, configuring councils, and the full tool menu. Call it with no argument for everything; every response lists the topics it accepts. |
| `ask_council` | Ask several models the SAME question; a synthesizer reconciles them (Fable by default — pass `synthesizer` to have e.g. `codex`/GPT-5.6 Sol adjudicate). For contentious, hard-to-reverse calls. Returns a consensus signal. Default council is fable+minimax, +deepseek when its API key is configured (cheap direct models first). Pass `provider="ollama"\|"atlas"\|"openrouter"\|"lmstudio"` to scope it to ONE gateway: that provider's configured panel and adjudicator ladder apply (GPT-first for atlas/openrouter), and `lmstudio` runs the panel sequentially (single GPU). |
| `ask_chain` | Sequential relay: draft → critique → decide down a pipeline (e.g. `m3 > fable`). Cost-tiered escalation. |
| `ask_debate` | Adversarial: proposer vs opponent over a claims ledger, a third model adjudicates (`adjudicator`, default Fable). The heaviest mode — reserve for genuine dilemmas. |
| `ask_verify` | Review a draft answer that ALREADY EXISTS — the only mode that takes a finished answer as input. Objections are classified by CODE: only ones it can check against your `context` or a failed check count as `prevented`; quoting the draft back at itself scores nothing. Never withholds or rewrites the draft. |
| `ask_falsify` | Stateful falsification ledger: assertor vs falsifier, resolved by a deterministic code clerk from `cite`/`contra` receipts; persists on the required `session`. `run:` sandbox receipts are opt-in (`ASK_FABLE_ALLOW_RUN=1` + `bwrap`); `metamorph=true` adds a stability check. |
| `ask_conference` | Divergent, multi-round brainstorm: several models argue a topic TOGETHER (each reads the running transcript and builds on/pushes back), then a rapporteur writes the map of the disagreement. Unlike a council (isolated answers), participants hear each other so positions move — for open-ended ideation. `models`/`rounds`/`synthesizer`; with no `models` and an elicitation-capable client, a NATIVE model picker pops up (`interactive:false` skips it). |

### Single models (independent of Fable)

All one-model calls go through **`ask_model(provider=…, model=…, question, context, context_ref, trusted, effort)`** — single-turn, guarded, cached. `provider` picks the backend; `model` overrides it where the backend accepts one (CLI/gateway providers). The per-backend tools below are the *legacy names* — still callable, no longer advertised.

| Provider | Backend |
|------|--------------|
| `minimax` (alias `m3`) | MiniMax-M3 alone. Cheap direct API. |
| `glm` | GLM-5.2 alone (Z.ai). |
| `deepseek` | deepseek-v4-pro alone (DeepSeek API). Cheap direct API — prefer over pricier cloud models. |
| `gemini` | Gemini 3.1 Pro alone (via local `agy` CLI). |
| `codex` (alias `gpt`) | GPT-5.6 Sol alone (via local `codex` CLI). |
| `sonnet` | Claude Sonnet 5 alone (same OAuth session as `ask`). |
| `grok` (alias `xai`) | Grok (grok-4.6) alone via local `grok` CLI — pass `model`/`effort` to override. Prefer over `atlas` with `xai/grok-*`. |
| `kimi` | Kimi (kimi-code/k3) alone via local `kimi` CLI on your Kimi Code subscription — pass `model`/`effort`. Sandboxed to pure text reasoning. Prompt capped near 131k bytes (argv); use `atlas` for bigger context. |
| `ollama` | One Ollama Cloud model alone (pass `model`, e.g. `gpt-oss:120b-cloud`). |
| `lmstudio` | One model on the operator's **LM Studio** server (LAN, no cloud key). Pass `model`. Loads a missing model explicitly with a real context window and never bumps a resident model off by default — a load that does not fit returns an `unload_offer`. Call `list_models(provider="lmstudio")` first. |
| `atlas` | One Atlas Cloud text model alone (pass `model`, optional `effort`). Call `list_models(provider="atlas")` first. Atlas's gateway 504s any request still generating at ~242s; `atlas_max_tokens`/`atlas_timeout` override the preset — see `docs/CONFIGURATION.md`. |
| `openrouter` | One OpenRouter model alone (pass `model`) — ~400 models from every major lab on one key. The catch-all. Call `list_models(provider="openrouter")` first. No fixed ~242s wall. |
| `ali` | One Alibaba Cloud (Qwen) reasoning model alone (pass `model`; default `qwen3.8-max`) — via the token-plan MaaS gateway's Anthropic surface, so `thinking` is captured. Needs `ASK_FABLE_ALI_API_KEY`. |

| `list_models` | One catalogue, selected by `provider`: `ali`, `atlas`, `openrouter`, `ollama`, `lmstudio`. Atlas/OpenRouter rank a task-aware shortlist (`task="…"`) and open a native model + effort picker when form elicitation is supported; `ali` takes `all=true`; `refresh=false` makes no network call. Read-only, free.

### Web search / OSINT (opt-in)

| Tool | What it does |
|------|--------------|
| `ask_websearch` | The **one** tool that browses. Runs a search-capable model with LIVE web search on to do a research / OSINT / current-facts task and return a sourced, cited answer. Pick the backend with `model`: `grok` (grok-4.6 live search, the default), a Claude model on the OAuth session — `sonnet` / `opus48` / `opus5` / `fable` (native `WebSearch`/`WebFetch`), or `gemini` (the local `agy` CLI, search-only). All flat-plan — no per-token cost. **Off by default:** the operator must set `ASK_FABLE_ALLOW_WEBSEARCH=1` (else `status:"disabled"`). On grok/Claude only `WebSearch`/`WebFetch` are enabled and everything else stays blocked; `gemini` is search-only because `agy`'s headless policy denies page fetch/shell and a denial kills the turn — so it gets an explicit "search_web ONLY" prompt. Uncached (web facts are time-sensitive). Every other `ask_*` tool is deliberately toolless. |

`opus` (aliases `opus5`, `opus-5`) is a first-class token in every multi-model
mode — council member or `synthesizer`, chain stage, debate
proposer/opponent/`adjudicator` — and it needs no extra configuration.

`fable51` (aliases `fable5.1`, `fable-5.1`) is the same, and pins
`claude-fable-5-1` even once the ladder has moved past it. Today it resolves to
the same model as `fable`, so pairing them in one council buys nothing. It is
left out of the `middle`/`full` tier presets for that reason.

`twin` — the **twin flames** (aliases `twins`, `twin flames`, `twin-flame`,
`twin_flames`) — is a model GROUP rather than a single model: it expands to
`fable` + `opus`, a dual Fable/Opus 5 invocation on the one OAuth session, with
no provider keys needed. Use it anywhere a LIST of models is taken —
`ask_council(models=["twin"])` or `ask_council(tier="twin")` for the pair in
parallel, `ask_chain(pipeline="m3 > twin")` for two stages, fable then opus.
Single-model slots (`synthesizer`, debate `proposer`/`opponent`/`adjudicator`)
refuse it with a `bad_args` error — name one member there instead.

Atlas, OpenRouter, and local LM Studio models can also join `ask_council`,
`ask_chain`, and `ask_debate` as `atlas:<model-id>` / `openrouter:<model-id>` /
`lmstudio:<model>` tokens. First call
`list_models(provider="atlas", task="review a risky database migration")`, then place one or
more returned IDs into a model list or pipeline, for example
`models=["fable", "atlas:deepseek-ai/deepseek-v4-pro", "atlas:zai-org/glm-5.2"]`.
`lmstudio:<model>` tokens are deliberately kept out of the tier presets.

### Context bus (point, don't paste)

| Tool | What it does |
|------|--------------|
| `context_read` | Read the shared context bus: pass `key` for that blob, or omit it to LIST every stored key (size/age/description, never the value). Read-only. |
| `context` | Mutate the bus, dispatched by `op`: `write` (store `value` under `key`), `pack` (read repo `paths` into a bundle under `key`, `path:START-END` allowed), `delete` (remove `key`). Destructive/overwriting. |

### Ops & observability

| Tool | What it does |
|------|--------------|
| `stats` | Usage/health from the audit log (calls / errors / latency). |
| `trace_list` | List recent correlated tool traces. |
| `trace_get` | Read the ordered events of one trace by id. |
| `reset_session` | Dump + clear a session (`model="fable"` for `ask`'s Fable threads, an Opus token for the Opus threads). |
| `list_models` | List a gateway's models: `provider="ali"\|"atlas"\|"openrouter"\|"ollama"\|"lmstudio"`. Atlas/OpenRouter rank a task-aware shortlist; lmstudio shows loaded/available models and a VRAM fit classification. |
| `unload_lms_model` | Free one resident LM Studio model — **operator action**, never without explicit consent; refuses while a chat is in flight and confirms the unload. |
| `host_status` | Read-only GPU/host status from the control panel `/api/state.json`: util, VRAM used/total/free, temp, power, holders, services, loaded models, warnings. Also powers the `ask_model(provider="lmstudio")` room check. |
| `diagnose` | Read-only health check of every reasoning backend (reachability, resolved model, timeout, circuit-breaker). No paid model call. |
| `configure_council` | Persist a provider council default: `provider="ollama"\|"atlas"\|"openrouter"`, plus `models`, and `synthesizer` (atlas/openrouter) or `default_model` (ollama). Writes config. |
| `configure_tracing` | Toggle reasoning traces (`trace_mode` safe/full) + live console thinking (`stream_reasoning`) at runtime; persisted, no restart. |
| `configure_disabled` | Turn oracles/providers OFF (or back on) at runtime — persisted, no restart; a disabled backend is dropped from every council/tier. |
| `code_index` / `code_search` | Build/refresh a local code+docs index, then search it for relevant windows (`file:start-end`) instead of grepping whole files. |
| `session_list` / `session_peek` / `session_stats` | Inspect the local visibility-only hub: list project-scoped sessions, read retained full turns by label, or aggregate health. `session_peek` spans matching labels across projects; treat it as sensitive. |
