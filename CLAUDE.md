# CLAUDE.md

Guidance for working in this repo.

## ask_fable tool menu

The `ask_fable` MCP server exposes strong external reasoning models as a second
brain for software/engineering work. Reach for `ask` liberally and early — before
guessing at unfamiliar code, when weighing a design trade-off, or to sanity-check
a plan or diff. The models can't see the repo, so put the real code in `context`
(or point at files with `context_pack`). Scope is software/engineering, including
conceptual/brainstorming questions (ideation, ideas for future code — no context
needed). Refused only when the question itself directly asks for
offensive-security work (exploit development, attack tooling) or non-software
domain knowledge (e.g. biology); questions about security-related code are fine.

> A local copy of this menu prints on demand via
> `python3 ~/.claude/ask_fable_menu.py`.

### Core reasoning

| Tool | What it does |
|------|--------------|
| `ask` | Default move. Ask Fable about your code/design. Put code in `context`; reuse `session` for follow-ups. Not pinned to an id — it asks for the newest Fable (today `claude-fable-5-1`) and steps down to `claude-fable-5` if the local Claude Code is too old for it. `ASK_FABLE_FABLE_MODEL` pins an exact id. |
| `ask_opus5` | Same tool on Claude Opus 5 (claude-opus-5) — identical args/results, same OAuth session, ~half Fable's price and faster. Prefer it for high-volume or long back-and-forth work; sessions are namespaced separately from `ask`. |
| `ask_council` | Ask several models the SAME question; a synthesizer reconciles them (Fable by default — pass `synthesizer` to have e.g. `codex`/GPT-5.6 Sol adjudicate). For contentious, hard-to-reverse calls. Returns a consensus signal. Default council is fable+minimax, +deepseek when its API key is configured (cheap direct models first). |
| `ask_chain` | Sequential relay: draft → critique → decide down a pipeline (e.g. `m3 > fable`). Cost-tiered escalation. |
| `ask_debate` | Adversarial: proposer vs opponent over a claims ledger, a third model adjudicates (`adjudicator`, default Fable). The heaviest mode — reserve for genuine dilemmas. |
| `ask_ollama_council` | Several Ollama Cloud models; Fable synthesizes. |
| `ask_atlas_council` | Several Atlas Cloud models; GPT-5.6 Sol adjudicates (local `codex` CLI preferred, else Atlas-hosted `openai/gpt-5.6-sol`, else Fable). Members default to the configured `atlas_council` set, else 3 featured catalog models. |
| `ask_openrouter_council` | Several OpenRouter models; same GPT-first adjudicator ladder. A cross-LAB panel (Claude + GPT + Gemini + DeepSeek) on one key. |

### Single models (independent of Fable)

| Tool | What it does |
|------|--------------|
| `ask_m3` | MiniMax-M3 alone. |
| `ask_glm` | GLM-5.2 alone (Z.ai). |
| `ask_deepseek` | deepseek-v4-pro alone (DeepSeek API). Cheap direct API — prefer over pricier cloud models. |
| `ask_gemini` | Gemini 3.1 Pro alone (via local `agy` CLI). |
| `ask_codex` | GPT-5.6 Sol alone (via local `codex` CLI). |
| `ask_grok` | Grok (grok-4.6) alone via local `grok` CLI — prefer over `ask_atlas` with `xai/grok-*`. |
| `ask_kimi` | Kimi (kimi-code/k3) alone via local `kimi` CLI on your Kimi Code subscription — prefer over `ask_atlas` with `moonshotai/kimi-*`. Sandboxed to pure text reasoning (no tools/filesystem). Prompt capped near 131k bytes by the CLI's argv transport; use `ask_atlas` for bigger context. |
| `ask_ollama` | One Ollama Cloud model alone (e.g. `gpt-oss:120b-cloud`). |
| `ask_atlas` | One Atlas Cloud text model alone. Call `list_atlas_models` first, then call with the selected model + effort. |
| `ask_openrouter` | One OpenRouter model alone — ~400 models from every major lab on one key. The catch-all for models with no dedicated tool. Call `list_openrouter_models` first. |
| `list_atlas_models` | Live Atlas catalog and task-aware recommender. Pass `task="…"` to rank a provider-diverse shortlist and open a native model + effort picker when form elicitation is supported. Catalog lookup is free. |
| `list_openrouter_models` | Live OpenRouter catalog (~400 models) with price, context and per-model reasoning support. Ranked from the catalog's own data, so new models rank correctly with no update here. Free. |

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

Atlas and OpenRouter models can also join `ask_council`, `ask_chain`, and
`ask_debate` as `atlas:<model-id>` / `openrouter:<model-id>` tokens. First call
`list_atlas_models(task="review a risky database migration")`, then place one or
more returned IDs into a model list or pipeline, for example
`models=["fable", "atlas:deepseek-ai/deepseek-v4-pro", "atlas:zai-org/glm-5.2"]`.

### Context bus (point, don't paste)

| Tool | What it does |
|------|--------------|
| `context_pack` | Name repo files (`path:START-END`); the server reads them into a bundle. Pass its key as `context_ref` on `ask`. |
| `context_write` | Store a blob under a key; reuse via `context_ref` instead of re-pasting. |
| `context_read` | Read a stored blob back by key. |
| `context_list` | List what's in the context store. |
| `context_delete` | Delete a stored blob by key. |

### Ops & observability

| Tool | What it does |
|------|--------------|
| `stats` | Usage/health from the audit log (calls / errors / latency). |
| `trace_list` | List recent correlated tool traces. |
| `trace_get` | Read the ordered events of one trace by id. |
| `reset_session` | Dump + clear a Fable conversation session. |
| `list_ollama_models` | Show available Ollama Cloud models + current council. |
| `configure_ollama_council` | Persist your chosen Ollama council (writes config). |
| `configure_atlas_council` | Persist your chosen Atlas council and/or its adjudicator (writes config; used by `ask_atlas_council`). |
| `configure_openrouter_council` | Same for `ask_openrouter_council`. |
| `configure_tracing` | Toggle reasoning traces (`trace_mode` safe/full) + live console thinking (`stream_reasoning`) at runtime; persisted, no restart. |
| `session_list` / `session_peek` / `session_stats` | Inspect the local visibility-only hub: list project-scoped sessions, read retained full turns by label, or aggregate health. `session_peek` spans matching labels across projects; treat it as sensitive. |
