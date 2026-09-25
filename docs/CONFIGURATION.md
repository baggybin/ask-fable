# Configuration reference

> Part of the [ask-fable README](../README.md).


Most installations only need a registered Fable bridge. Configure an optional
backend, persistence, or trace limit only when you need it; the full reference is
grouped below for operators.

| Var | Default | Meaning |
|---|---|---|
| `ASK_FABLE_MIN_LEN` / `ASK_FABLE_MAX_LEN` | 3 / 65536 | question length bounds |
| `ASK_FABLE_MAX_CONTEXT_LEN` | off (unbounded) | optional context cap; any value is floored to **512,000** chars |
| `ASK_FABLE_TIMEOUT` | 240 | per-turn wall-clock seconds |
| `ASK_FABLE_MAX_NEEDS_CONTEXT` | 2 | consecutive `needs_more_context` turns on a session before `ask` returns `context_exhausted` (0 = stop after the first) |
| `ASK_FABLE_FABLE_TRANSPORT` | `auto` | where an Anthropic turn (Fable/Opus/Sonnet family) may run: `auto` = the Claude Code ladder (SDK, then the `claude` CLI), `sdk` / `cli` / `http` pin one transport and never fall back. `http` is the **Anthropic Messages API with your own key** — for a host with no Claude Code; it is deliberately NOT on the `auto` ladder, because an exported key must never turn a flat-plan oracle into a per-token one without you asking. An unrecognised value is an error, not a silent `auto`. A pinned transport that cannot do the job (no key, or a follow-up on an `http` session — that transport keeps no server-side conversation, so a `resume=` there is `transport_incapable`) fails with the reason rather than degrading. `sdk` and `cli` both resume |
| `ASK_FABLE_ANTHROPIC_API_KEY` | unset | the key for `ASK_FABLE_FABLE_TRANSPORT=http`. Without it that transport is `not_configured` (and `diagnose` says so) |
| `ASK_FABLE_ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | override the endpoint the `http` transport posts to. Note this sends the key to whatever host it names |
| `ASK_FABLE_USE_CLI` | off | **superseded** by `ASK_FABLE_FABLE_TRANSPORT` and still honored as an alias (`1` → `cli`, `0` → `sdk`); the named transport wins if both are set, and a disagreement is reported back as `transport_warning`. Kept for existing setups |
| `ASK_FABLE_FABLE_MODEL` | unset (ladder) | pin the exact Fable id for `ask` and the `fable` oracle, skipping the newest-first ladder (`claude-fable-5-1` → `claude-fable-5`). A pinned call never falls back — if that id can't run, the turn fails and says so |
| `ANTHROPIC_API_KEY` | unset | **not an ask_fable setting, but it changes how the OAuth transports authenticate.** Claude Code treats this variable as env-based auth and prefers it over the Claude Code session (`claude_agent_sdk/_internal/session_resume.py` skips the OAuth credential hand-off when it is present). If you export it, a "flat plan" turn may in fact be billed per token while `ask_fable` still labels it `cost_basis="subscription"` — unset it in the MCP server's `env` if you want the OAuth session billed. |
| `ASK_FABLE_CLAUDE_CLI` | unset (auto) | pin the Claude Code binary the Agent SDK spawns. By default the SDK prefers the copy vendored inside `claude-agent-sdk`, which can be months behind the one on your PATH and too old for a newly released model; the bridge hands it the PATH binary instead when that one is strictly newer |
| `ASK_FABLE_MINIMAX_MODEL` | `MiniMax-M3` | model id for the `ask_council` MiniMax oracle |
| `ASK_FABLE_GEMINI_MODEL` | `Gemini 3.1 Pro (High)` | `agy` model name for `ask_model(provider="gemini")` / the `gemini` council oracle (run `agy models` to list; via the `agy` CLI) |
| `ASK_FABLE_GEMINI_TIMEOUT` | falls back to `ASK_FABLE_TIMEOUT`, else 240 | per-turn seconds for the `agy`/Gemini oracle specifically — cap this agentic CLI without lowering the global timeout. On timeout the whole `agy` process group is SIGKILLed (it spawns children), so a slow turn can't hang the call or leak orphans |
| `ASK_FABLE_CODEX_MODEL` | `gpt-5.6-sol` | model id for `ask_model(provider="codex")` / the `codex` council oracle (via the `codex` CLI) |
| `ASK_FABLE_CODEX_REASONING` | `high` | reasoning effort passed to `codex exec` (`model_reasoning_effort`) |
| `ASK_FABLE_CODEX_TIMEOUT` | falls back to `ASK_FABLE_TIMEOUT`, else 240 | per-turn seconds for the `codex` oracle specifically. On timeout the whole `codex` process group is SIGKILLed (it spawns children), so a slow turn can't hang the call or leak orphans |
| `ASK_FABLE_GROK_MODEL` / `ASK_FABLE_GROK_REASONING` / `ASK_FABLE_GROK_TIMEOUT` | `grok-4.6` / `low` / falls back to `ASK_FABLE_TIMEOUT` | local `grok` CLI settings. `quick`, `standard`, and `deep` effort presets map to low reasoning to keep context-heavy turns bounded; set `ASK_FABLE_GROK_REASONING` explicitly for Grok-native medium/high |
| `ASK_FABLE_KIMI_MODEL` / `ASK_FABLE_KIMI_EFFORT` / `ASK_FABLE_KIMI_TIMEOUT` / `ASK_FABLE_KIMI_HOME` | `kimi-code/k3` / `high` / falls back to `ASK_FABLE_TIMEOUT` / `~/.kimi-code` | local `kimi` CLI settings. Effort accepts `low`/`high`/`max` plus the `quick`/`standard`/`deep` presets. The prompt travels as one argv value, so prompts above ~120k bytes are refused with `context_too_large` — use `ask_model(provider="atlas", model="moonshotai/kimi-k3")` for bigger context |
| `ASK_FABLE_ALLOW_WEBSEARCH` | unset (**off**) | opt-in gate for the `ask_websearch` research tool — the only `ask_*` tool that browses the live web. `1`/`true`/`yes`/`on` enables it; unset returns `{"status":"disabled",...}`. Reads config-file-then-env, so a `configure_*`-style config write can toggle it at runtime |
| `ASK_FABLE_WEBSEARCH_MODEL` | `grok` | default backend for `ask_websearch` when the caller omits `model`. One of `grok` (grok-4.6 live search) / `gemini` (local `agy` CLI, **search-only** — see the note below; aliases `agy`, `gemini-3.1-pro`) / `sonnet` / `opus48` / `opus5` / `fable` (Claude native WebSearch over the OAuth session). Also settable as config key `websearch_model` |
| `ASK_FABLE_WEBSEARCH_MAX_TURNS` | `20` | search budget for `ask_websearch` (min 2). For the `grok` backend it is the agentic turn count; for the `http` transport it maps to the server-side tool's `max_uses`, so it is also the **cost ceiling** — the API bills $10 per 1,000 searches, i.e. ≤ $0.20 a call at the default. `agy` has no equivalent flag (its `gemini` backend is search-only) |
| `ASK_FABLE_GEMINI_*` | see above | the `gemini` websearch backend reuses the `ask_model(provider="gemini")` knobs (`ASK_FABLE_GEMINI_MODEL` / `_TIMEOUT`). It is **search-only by design**: `agy` runs print mode under its own headless permission policy, which allows `search_web` but auto-denies page fetch, shell, and file/dir tools — and a denial aborts the turn with empty output, so `ask_websearch` hands that backend a "`search_web` ONLY" prompt. `agy` defines no `--allowed-tools`/`--disallowed-tools`/`--permission-mode`, so there is no way for ask_fable to widen or narrow it; use `grok` or a Claude model when the task needs page content |
| `ASK_FABLE_CLI_MAX_PARALLEL` | 2 | maximum concurrent local CLI processes **per binary** (`claude`, `mmx`, `grok`, `codex`, `agy`, `kimi`); `0` or negative disables this gate and `1` serializes each CLI family. The queue wait counts against the call's own timeout, so a call parked behind busy slots fails as a timeout instead of waiting unboundedly |
| `ASK_FABLE_GLM_API_KEY` | — | Z.ai key that enables the `glm` council oracle (unset = oracle unavailable) |
| `ASK_FABLE_GLM_BASE_URL` / `_MODEL` | `https://api.z.ai/api/anthropic` / `glm-5.2` | GLM endpoint + model |
| `ASK_FABLE_DEEPSEEK_API_KEY` | — | DeepSeek key that enables the `deepseek` council oracle |
| `ASK_FABLE_DEEPSEEK_BASE_URL` / `_MODEL` | `https://api.deepseek.com/anthropic` / `deepseek-v4-pro` | DeepSeek endpoint + model |
| `ASK_FABLE_ATLAS_API_KEY` / `ATLASCLOUD_API_KEY` | — | Atlas Cloud key for HTTP `ask_model(provider="atlas")` and `atlas:<model-id>` calls (either name is accepted); local `xai/grok-*` routes reuse the authenticated `grok` CLI |
| `ASK_FABLE_ATLAS_BASE_URL` | `https://api.atlascloud.ai` | Atlas Cloud API base URL; override only with a trusted compatible endpoint because it receives the bearer key and request content |
| `ASK_FABLE_ATLAS_MODEL` | `xai/grok-4.6` | default model for `ask_model(provider="atlas")` when no model is passed |
| `ASK_FABLE_OPENROUTER_API_KEY` / `OPENROUTER_API_KEY` | — | OpenRouter key for `ask_model(provider="openrouter")`, `ask_council(provider="openrouter")` and `openrouter:<model-id>` tokens (either name is accepted; the catalog needs no key) |
| `ASK_FABLE_OPENROUTER_MODEL` | `deepseek/deepseek-v4.1-flash` | default model for `ask_model(provider="openrouter")` when none is passed |
| `ASK_FABLE_OPENROUTER_COUNCIL` / `_SYNTHESIZER` / `_EFFORT` | — | default panel, adjudicator and effort for `ask_council(provider="openrouter")` (config keys `openrouter_council` / `openrouter_synthesizer` / `openrouter_effort` win) |
| `ASK_FABLE_ATLAS_EFFORT` / `ASK_FABLE_EFFORT` | `deep` | default Atlas effort (`quick`, `standard`, or `deep`); `atlas_effort` / `effort` in the config file override environment values |
| `ASK_FABLE_ATLAS_MAX_TOKENS` / `ASK_FABLE_ATLAS_TIMEOUT` | unset (effort preset) | per-call output cap / wall-clock seconds for Atlas requests, replacing the effort preset's values (config keys `atlas_max_tokens` / `atlas_timeout` win over the env); the cap still respects `ASK_FABLE_MAX_TOKENS`. Atlas's gateway returns HTTP 504 for a request still running at ~242s, so pinning these (e.g. `8192` / `240`) fits long generations inside the window |
| `ASK_FABLE_ATLAS_COUNCIL` | — | default members for `ask_council(provider="atlas")` (comma/space list of Atlas model ids; config file `atlas_council` overrides); unset → 3 featured catalog models |
| `ASK_FABLE_ATLAS_SYNTHESIZER` | — | adjudicator for `ask_council(provider="atlas")` (any council token; config file `atlas_synthesizer` overrides); unset → local `codex` CLI → `atlas:openai/gpt-5.6-sol` → `fable` |
| `ASK_FABLE_OLLAMA_API_KEY` | — | only for a **remote** endpoint (`ollama.com`); the default local daemon needs no key |
| `ASK_FABLE_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint (POSTs `/api/chat`). Default is the local daemon, which proxies `:cloud` models via `ollama signin`; set `https://ollama.com` (+ key) for direct cloud |
| `ASK_FABLE_OLLAMA_MODEL` | `gpt-oss:120b-cloud` | default model for `ask_model(provider="ollama")` when none is passed (config file `ollama_model` overrides) |
| `ASK_FABLE_OLLAMA_COUNCIL` | `minimax-m3:cloud, glm-5.2:cloud, nemotron-3-ultra:cloud, qwen3-coder:480b-cloud, kimi-k2.7-code:cloud, deepseek-v4-pro:cloud, gpt-oss:120b-cloud` | models for the `full` tier + default `ask_council(provider="ollama")` (comma/space list; config file `ollama_council` overrides) |
| `ASK_FABLE_LMSTUDIO_BASE_URL` | `http://lmstudio.example.com:1234` | LM Studio server root for `ask_model(provider="lmstudio")`, `ask_council(provider="lmstudio")`, `list_models(provider="lmstudio")`, `unload_lms_model` and `lmstudio:<model>` tokens (LAN, no cloud key) |
| `ASK_FABLE_LMSTUDIO_API_KEY` | — | bearer token, only when the LM Studio server requires one |
| `ASK_FABLE_LMSTUDIO_MODEL` | — | default model for `ask_model(provider="lmstudio")` when none is passed (config `lmstudio_model` overrides); unset falls back to the single resident model |
| `ASK_FABLE_LMSTUDIO_CONTEXT` | 32768 | context window requested on an explicit load (config `lmstudio_context`); raised to fit the prompt, capped to the model max — never the app-wide 4k default. A per-host ceiling in config `lmstudio_context_ceilings` (e.g. `{"lmstudio-host": 32768}`, `"default"` as fallback) always wins, so a small box whose engine aborts on an oversized KV allocation can never be asked for one |
| `ASK_FABLE_LMSTUDIO_MAX_TOKENS` | 8192 | output cap for one local generation (config `lmstudio_max_tokens`); separate from `ASK_FABLE_MAX_TOKENS` because local generation is slow |
| `ASK_FABLE_LMSTUDIO_SWAP` | `never` | `never` = ask-first: a load that does not fit returns an `unload_offer` and the operator decides. `auto` = unload the blocking model, wait for confirmation, reload, restore on failure (config `lmstudio_swap`) |
| `ASK_FABLE_LMSTUDIO_COUNCIL` | `qwen/qwen3.6-35b-a3b, qwen/qwen3.8-27b, qwen/qwen3.5-9b, zai-org/glm-4.6v-flash, google/gemma-4-31b-qat` | default panel for `ask_council(provider="lmstudio")` (config `lmstudio_council`) — the five fastest strong locals from the 2026-09-13 sweep |
| `ASK_FABLE_LMSTUDIO_TIMEOUT` / `_LOAD_TIMEOUT` / `_UNLOAD_WAIT` | 600 / 600 / 180 | seconds: one local completion, one load, one unload-confirmation wait |
| `ASK_FABLE_CONTROL_URL` | `http://192.0.2.10:5000` | control-panel JSON (`/api/state.json`) powering `host_status` and the `ask_model(provider="lmstudio")` VRAM room check (config `control_page`); unreachable = best-effort fallback, never fatal. The room check trusts the page only when it monitors the same machine as `ASK_FABLE_LMSTUDIO_BASE_URL` (host match by resolution) — otherwise "unknown", never another box's VRAM numbers |
| `ASK_FABLE_CONFIG_FILE` | `${XDG_CONFIG_HOME:-~/.config}/ask_fable/config.json` | tool-writable config (`ollama_council`, `ollama_model` via `configure_council(provider="ollama")`; `atlas_council`, `atlas_synthesizer` via `configure_council(provider="atlas")`; `ASK_FABLE_TRACE_MODE`, `ASK_FABLE_STREAM_REASONING` via `configure_tracing`); overrides the matching env vars |
| `ASK_FABLE_OLLAMA_CATALOG_URL` | `https://ollama.com` | where `list_models(provider="ollama")` fetches the cloud catalog (`/api/tags`) |
| `ASK_FABLE_MAX_TOKENS` | 65536 | max output tokens for GLM/DeepSeek, Ollama (`num_predict`), and MiniMax (`--max-tokens`); Fable uses the model default |
| `ASK_FABLE_QUIET` | off | silence the stderr progress/reasoning trace |
| `ASK_FABLE_SHOW_REASONING` | on | show model reasoning excerpts in the trace |
| `ASK_FABLE_STREAM_REASONING` | off | live-stream Fable's reasoning block-by-block to the stderr trace as it arrives (Fable only; other backends don't stream) |
| `ASK_FABLE_RETURN_THINKING` | off | attach a capped reasoning excerpt (`thinking`) to the tool result body so it renders inline in the client |
| `ASK_FABLE_THINKING_CHARS` | 4000 | cap for the `ASK_FABLE_RETURN_THINKING` excerpt |
| `ASK_FABLE_DENYLIST_FILE` | — | extra denylist terms (one per line) for the fallback |
| `ASK_FABLE_ALLOWLIST_FILE` | — | benign phrases (one per line) neutralized before matching, to rescue false positives like `request payload`; rescues only the exact phrase |
| `ASK_FABLE_ALLOW_RUN` | off | opt-in gate for executing model-authored Python, used by **`ask_falsify`** (`run:` receipts) **and `ask_verify`** (a reviewer's `run` receipt). The snippet runs in a bubblewrap sandbox (no network, no filesystem, stdlib only). Requires `bwrap`; without the flag or `bwrap` nothing is executed and the receipt is inconclusive, never a finding. **One flag turns it on for both tools** — there is no per-tool switch |
| `ASK_FABLE_ALLOW_TRUSTED` | off | operator authorization for the `trusted=true` argument: when on, the prohibited-use denylist runs log-only (question **and** `context` audited, not blocked) and the audit records the marker; otherwise the flag is ignored |
| `ASK_FABLE_GUARD_SCAN_CONTEXT` | **on** | whether the Layer-2 denylist also runs over `context` (the question is always scanned). Default **on**: the provider's own safeguard reads the whole payload, so framing parked in `context` would be refused upstream — scanning it here makes the block deterministic and local. Set to `0`/`false`/`no`/`off` to scan the question only. Config file overrides env, so it is runtime-togglable. `trusted=true` lifts it |
| `ASK_FABLE_PROJECT_ROOT` | — | project root that `context(op="pack")` may read from; **unset disables `context(op="pack")`** (returns `not_configured`). Reads never escape this root |
| `ASK_FABLE_PACK_MAX_CHARS` | 24000 | default total-character budget for a `context(op="pack")` bundle (over-budget specs are reported in `skipped`, never truncated) |
| `ASK_FABLE_PACK_MAX_FILES` | 32 | max files admitted in one `context(op="pack")` |
| `ASK_FABLE_PACK_MAX_FILE_BYTES` | 1000000 | per-file read cap for `context(op="pack")` (a whole file over this is skipped `too_large`; a line-range is capped on bytes collected) |
| `ASK_FABLE_EMBED_HOSTS` | LM Studio host | ordered comma-separated embedding hosts for `code_index`/`code_search` (each an OpenAI-compatible root, e.g. `http://lmstudio-host:1234,http://lmstudio.example.com:1234`). The explicit list IS the opt-in — a part-time eGPU box is used only when named; a dead host is skipped for the next, and when none answers search degrades to keyword |
| `ASK_FABLE_EMBED_MODEL` | `text-embedding-nomic-embed-text-v1.5` | embedding model for the index and queries; changing it re-embeds on the next `code_index` (vectors are stored per model and never mixed) |
| `ASK_FABLE_EMBED_API_KEY` | falls back to `ASK_FABLE_LMSTUDIO_API_KEY` | bearer token for the embedding hosts |
| `ASK_FABLE_EMBED_RERANK_MODEL` | — | opt-in chat model for `code_search(rerank=true)`; unset (or unreachable) = rerank skipped with a note, ranking untouched |
| `ASK_FABLE_AUDIT_PATH` | `$XDG_STATE_HOME/ask_fable/decisions.jsonl` | audit log |
| `ASK_FABLE_AUDIT_RAW` | off | store raw questions (and raw context unless overridden); otherwise store SHA-256 metadata only |
| `ASK_FABLE_AUDIT_RAW_CONTEXT` | follows `ASK_FABLE_AUDIT_RAW` | split switch for `context_raw` only — set `0` with `AUDIT_RAW=1` to keep raw questions for debugging while context (the larger proprietary-code / secret-bearing surface) stays hashed-only |
| `ASK_FABLE_CACHE` | on | cache successful single-shot/council answers to spare re-ask loops; set `0` to disable |
| `ASK_FABLE_CACHE_TTL` | 3600 | cache freshness window in seconds |
| `ASK_FABLE_CACHE_PATH` | `$XDG_STATE_HOME/ask_fable/cache.db` | SQLite cache location |
| `ASK_FABLE_CACHE_MAX_ROWS` | 10000 | row cap for the answer cache — a periodic sweep (every ~100 writes) deletes TTL-expired rows and trims to 90% of the cap, oldest first |
| `ASK_FABLE_CIRCUIT_BREAKER` | on | per-oracle circuit breaker: a chronically-failing backend is auto-skipped in council/chain fan-out (reported as `circuit_open` in `sources`, like `not_configured`); cache hits are still served. Never trips on `refused` or on config states (`not_configured`). Set `0` to disable |
| `ASK_FABLE_BREAKER_WINDOW` | 20 | last N outcomes tracked per oracle |
| `ASK_FABLE_BREAKER_THRESHOLD` | 0.5 | error rate over the window that opens the breaker (min 5 samples) |
| `ASK_FABLE_BREAKER_COOLDOWN` | 300 | seconds an open breaker waits before allowing a half-open probe; a probe success closes it and clears the window |
| `ASK_FABLE_CONTEXT_PATH` | `$XDG_STATE_HOME/ask_fable/context.db` | SQLite store for the context bus (`context(op="write")`/`context_ref`) |
| `ASK_FABLE_HUB` | on | set `0`, `false`, `no`, or `off` to disable the cross-instance session hub entirely |
| `ASK_FABLE_HUB_PATH` | `$XDG_STATE_HOME/ask_fable/hub.db` | local SQLite hub database; point it at shared storage only when every reader is trusted |
| `ASK_FABLE_HUB_MAX_ROWS` | 10000 | total retained hub-turn cap; a periodic oldest-first sweep trims history toward 90% of the cap |
| `ASK_FABLE_HUB_STALE_SECONDS` | 300 | heartbeat age after which `session_list` considers a session stale |
| `ASK_FABLE_HUB_PREVIEW_CHARS` | 160 | maximum `last_question` preview length returned by `session_list` |
| `ASK_FABLE_AGENT_ID` | inferred from the MCP client | explicit hub attribution label; use it to distinguish local windows/agents when client metadata is not unique |
| `ASK_FABLE_TRACE_MODE` | `safe` | `safe` stores correlated metadata only; `full` additionally stores redacted, size-capped trace bundles and answer Markdown |
| `ASK_FABLE_TRACE_DIR` | `$XDG_STATE_HOME/ask_fable/traces` | directory for full-mode trace bundles |
| `ASK_FABLE_TRACE_MAX_CONTENT_BYTES` | 104857600 (100 MiB) | maximum captured content per full trace bundle; truncation is recorded |
| `ASK_FABLE_TRACE_MAX_EVENT_BYTES` | 1048576 (1 MiB) | maximum JSONL event-line size accepted while reading traces; oversized lines are discarded safely |
| `ASK_FABLE_TRACE_QUERY_MAX_EVENTS` / `ASK_FABLE_TRACE_QUERY_MAX_BYTES` | 100000 / 52428800 (50 MiB) | upper bounds for one `trace_list` or `trace_get` scan |
| `ASK_FABLE_PROJECT_ID` | derived from the working directory | stable project label stored with each trace; set explicitly to correlate calls across working directories |
| `ASK_FABLE_SAVE` | unset | explicit `1` persists answer Markdown and explicit `0` disables it; when unset, Markdown is written only in full trace mode |
| `ASK_FABLE_OUTPUT_DIR` | `$XDG_STATE_HOME/ask_fable/answers` | where saved answers are written (0600 files, 0700 dir) |
| `ASK_FABLE_MAX_ANSWERS` | 0 (unlimited) | retention cap on saved answer Markdown files; only files ask-fable itself wrote (its own filename shape) are ever pruned. **The default answers dir is shared per user** — a cap set by one agent prunes the shared archive for all agents/projects |
| `ASK_FABLE_MAX_SESSIONS` | 0 (unlimited) | retention cap on session transcript dumps; same ownership filter and shared-dir caveat as `ASK_FABLE_MAX_ANSWERS` |
| `ASK_FABLE_COUNCIL_TIMEOUT` | `ASK_FABLE_TIMEOUT + 120` | upper bound (sec) on the `ask_council` panel fan-out — bounds the worst case where a backend swallows its own inner timeout. Oracles that already answered are preserved and synthesized; still-running ones are cancelled and shown as `kind:"timeout"` in `sources`. Only an all-timeout council surfaces `status:"error", kind:"timeout"`. Not a whole-call cap: synthesis (and its Fable retry) runs after the panel under the synthesizer's own per-call timeout, and the sequential `provider="lmstudio"` council applies the cap to each member in turn |
| `ASK_FABLE_CHAIN_TIMEOUT` | `max(600, n × ASK_FABLE_TIMEOUT)` (min 10) | hard upper bound (sec) on the `ask_chain` stage pipeline (the chain is sequential, so the default scales with the number of stages); the Fable fallback synthesis after a failed final stage runs after it, under Fable's own timeout. Surfaces as `status:"error", kind:"timeout"` with the partial `stages[]` collected so far |
| `ASK_FABLE_MAX_PARALLEL` | 6 | semaphore size for council fan-out — bounds simultaneous sockets on the `full` tier so a 12-model fan-out can't exhaust `ulimit -n` |
| `ASK_FABLE_AUDIT_MAX_BYTES` | 52428800 (50 MB) | size cap for the audit log; rotated to `decisions.<timestamp>.<seq>.jsonl` when exceeded |
| `ASK_FABLE_AUDIT_BACKUPS` | unlimited | optional cap on rotated audit segments; set `0` to discard the active segment on rotation |

Remote context bus (LAN) variables: `ASK_FABLE_CONTEXT_BUS` (unset = local store
only), `ASK_FABLE_CONTEXT_KEYRING`, `ASK_FABLE_CONTEXT_BUS_TOKEN_FILE`,
`ASK_FABLE_CONTEXT_BUS_TIMEOUT`, `ASK_FABLE_CONTEXT_MAX_BYTES`,
`ASK_FABLE_CONTEXT_BUS_DB`, `ASK_FABLE_CONTEXT_BUS_SOCKET`, `ASK_FABLE_CONTEXT_HWM`,
`ASK_FABLE_MACHINE_ID`, `ASK_FABLE_MODEL_ID` — full table and setup in
[REMOTE_CONTEXT_BUS.md](REMOTE_CONTEXT_BUS.md#configuration-reference).

Default-created persisted state (cache, context bus, hub, audit log, saved answers,
session dumps, and full-trace bundles) is written to a per-user state dir, with
newly created files mode `0600` and parent dirs mode `0700`. SQLite stores
(`cache.db`, `context.db`, `hub.db`) use WAL journal mode for crash safety. Markdown dumps (saved answers,
session transcripts) and the separately located config file go through an atomic
`tempfile + os.replace + fsync` so a crash mid-write can never leave a
partial or empty file on disk.
