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
| `ASK_FABLE_USE_CLI` | off | force the `claude` CLI bridge instead of the SDK |
| `ASK_FABLE_FABLE_MODEL` | unset (ladder) | pin the exact Fable id for `ask` and the `fable` oracle, skipping the newest-first ladder (`claude-fable-5-1` → `claude-fable-5`). A pinned call never falls back — if that id can't run, the turn fails and says so |
| `ASK_FABLE_CLAUDE_CLI` | unset (auto) | pin the Claude Code binary the Agent SDK spawns. By default the SDK prefers the copy vendored inside `claude-agent-sdk`, which can be months behind the one on your PATH and too old for a newly released model; the bridge hands it the PATH binary instead when that one is strictly newer |
| `ASK_FABLE_MINIMAX_MODEL` | `MiniMax-M3` | model id for the `ask_council` MiniMax oracle |
| `ASK_FABLE_GEMINI_MODEL` | `Gemini 3.1 Pro (High)` | `agy` model name for the `ask_gemini` tool / `gemini` council oracle (run `agy models` to list; via the `agy` CLI) |
| `ASK_FABLE_GEMINI_TIMEOUT` | falls back to `ASK_FABLE_TIMEOUT`, else 240 | per-turn seconds for the `agy`/Gemini oracle specifically — cap this agentic CLI without lowering the global timeout. On timeout the whole `agy` process group is SIGKILLed (it spawns children), so a slow turn can't hang the call or leak orphans |
| `ASK_FABLE_CODEX_MODEL` | `gpt-5.6-sol` | model id for the `ask_codex` tool / `codex` council oracle (via the `codex` CLI) |
| `ASK_FABLE_CODEX_REASONING` | `high` | reasoning effort passed to `codex exec` (`model_reasoning_effort`) |
| `ASK_FABLE_CODEX_TIMEOUT` | falls back to `ASK_FABLE_TIMEOUT`, else 240 | per-turn seconds for the `codex` oracle specifically. On timeout the whole `codex` process group is SIGKILLed (it spawns children), so a slow turn can't hang the call or leak orphans |
| `ASK_FABLE_GROK_MODEL` / `ASK_FABLE_GROK_REASONING` / `ASK_FABLE_GROK_TIMEOUT` | `grok-4.6` / `low` / falls back to `ASK_FABLE_TIMEOUT` | local `grok` CLI settings. `quick`, `standard`, and `deep` effort presets map to low reasoning to keep context-heavy turns bounded; set `ASK_FABLE_GROK_REASONING` explicitly for Grok-native medium/high |
| `ASK_FABLE_KIMI_MODEL` / `ASK_FABLE_KIMI_EFFORT` / `ASK_FABLE_KIMI_TIMEOUT` / `ASK_FABLE_KIMI_HOME` | `kimi-code/k3` / `high` / falls back to `ASK_FABLE_TIMEOUT` / `~/.kimi-code` | local `kimi` CLI settings. Effort accepts `low`/`high`/`max` plus the `quick`/`standard`/`deep` presets. The prompt travels as one argv value, so prompts above ~120k bytes are refused with `context_too_large` — use `ask_atlas` with `moonshotai/kimi-k3` for bigger context |
| `ASK_FABLE_CLI_MAX_PARALLEL` | 2 | maximum concurrent local CLI processes **per binary** (`claude`, `mmx`, `grok`, `codex`, `agy`, `kimi`); `0` or negative disables this gate and `1` serializes each CLI family. The queue wait counts against the call's own timeout, so a call parked behind busy slots fails as a timeout instead of waiting unboundedly |
| `ASK_FABLE_GLM_API_KEY` | — | Z.ai key that enables the `glm` council oracle (unset = oracle unavailable) |
| `ASK_FABLE_GLM_BASE_URL` / `_MODEL` | `https://api.z.ai/api/anthropic` / `glm-5.2` | GLM endpoint + model |
| `ASK_FABLE_DEEPSEEK_API_KEY` | — | DeepSeek key that enables the `deepseek` council oracle |
| `ASK_FABLE_DEEPSEEK_BASE_URL` / `_MODEL` | `https://api.deepseek.com/anthropic` / `deepseek-v4-pro` | DeepSeek endpoint + model |
| `ASK_FABLE_ATLAS_API_KEY` / `ATLASCLOUD_API_KEY` | — | Atlas Cloud key for HTTP `ask_atlas` and `atlas:<model-id>` calls (either name is accepted); local `xai/grok-*` routes reuse the authenticated `grok` CLI |
| `ASK_FABLE_ATLAS_BASE_URL` | `https://api.atlascloud.ai` | Atlas Cloud API base URL; override only with a trusted compatible endpoint because it receives the bearer key and request content |
| `ASK_FABLE_ATLAS_MODEL` | `xai/grok-4.6` | default model for `ask_atlas` when no model is passed |
| `ASK_FABLE_OPENROUTER_API_KEY` / `OPENROUTER_API_KEY` | — | OpenRouter key for `ask_openrouter`, `ask_openrouter_council` and `openrouter:<model-id>` tokens (either name is accepted; the catalog needs no key) |
| `ASK_FABLE_OPENROUTER_MODEL` | `deepseek/deepseek-v4.1-flash` | default model for `ask_openrouter` when none is passed |
| `ASK_FABLE_OPENROUTER_COUNCIL` / `_SYNTHESIZER` / `_EFFORT` | — | default panel, adjudicator and effort for `ask_openrouter_council` (config keys `openrouter_council` / `openrouter_synthesizer` / `openrouter_effort` win) |
| `ASK_FABLE_ATLAS_EFFORT` / `ASK_FABLE_EFFORT` | `deep` | default Atlas effort (`quick`, `standard`, or `deep`); `atlas_effort` / `effort` in the config file override environment values |
| `ASK_FABLE_ATLAS_COUNCIL` | — | default members for `ask_atlas_council` (comma/space list of Atlas model ids; config file `atlas_council` overrides); unset → 3 featured catalog models |
| `ASK_FABLE_ATLAS_SYNTHESIZER` | — | adjudicator for `ask_atlas_council` (any council token; config file `atlas_synthesizer` overrides); unset → local `codex` CLI → `atlas:openai/gpt-5.6-sol` → `fable` |
| `ASK_FABLE_OLLAMA_API_KEY` | — | only for a **remote** endpoint (`ollama.com`); the default local daemon needs no key |
| `ASK_FABLE_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint (POSTs `/api/chat`). Default is the local daemon, which proxies `:cloud` models via `ollama signin`; set `https://ollama.com` (+ key) for direct cloud |
| `ASK_FABLE_OLLAMA_MODEL` | `gpt-oss:120b-cloud` | default model for `ask_ollama` when none is passed (config file `ollama_model` overrides) |
| `ASK_FABLE_OLLAMA_COUNCIL` | `minimax-m3:cloud, glm-5.2:cloud, nemotron-3-ultra:cloud, qwen3-coder:480b-cloud, kimi-k2.7-code:cloud, deepseek-v4-pro:cloud, gpt-oss:120b-cloud` | models for the `full` tier + default `ask_ollama_council` (comma/space list; config file `ollama_council` overrides) |
| `ASK_FABLE_CONFIG_FILE` | `${XDG_CONFIG_HOME:-~/.config}/ask_fable/config.json` | tool-writable config (`ollama_council`, `ollama_model` via `configure_ollama_council`; `atlas_council`, `atlas_synthesizer` via `configure_atlas_council`; `ASK_FABLE_TRACE_MODE`, `ASK_FABLE_STREAM_REASONING` via `configure_tracing`); overrides the matching env vars |
| `ASK_FABLE_OLLAMA_CATALOG_URL` | `https://ollama.com` | where `list_ollama_models` fetches the cloud catalog (`/api/tags`) |
| `ASK_FABLE_MAX_TOKENS` | 65536 | max output tokens for GLM/DeepSeek, Ollama (`num_predict`), and MiniMax (`--max-tokens`); Fable uses the model default |
| `ASK_FABLE_QUIET` | off | silence the stderr progress/reasoning trace |
| `ASK_FABLE_SHOW_REASONING` | on | show model reasoning excerpts in the trace |
| `ASK_FABLE_STREAM_REASONING` | off | live-stream Fable's reasoning block-by-block to the stderr trace as it arrives (Fable only; other backends don't stream) |
| `ASK_FABLE_RETURN_THINKING` | off | attach a capped reasoning excerpt (`thinking`) to the tool result body so it renders inline in the client |
| `ASK_FABLE_THINKING_CHARS` | 4000 | cap for the `ASK_FABLE_RETURN_THINKING` excerpt |
| `ASK_FABLE_DENYLIST_FILE` | — | extra denylist terms (one per line) for the fallback |
| `ASK_FABLE_ALLOWLIST_FILE` | — | benign phrases (one per line) neutralized before matching, to rescue false positives like `request payload`; rescues only the exact phrase |
| `ASK_FABLE_PROJECT_ROOT` | — | project root that `context_pack` may read from; **unset disables `context_pack`** (returns `not_configured`). Reads never escape this root |
| `ASK_FABLE_PACK_MAX_CHARS` | 24000 | default total-character budget for a `context_pack` bundle (over-budget specs are reported in `skipped`, never truncated) |
| `ASK_FABLE_PACK_MAX_FILES` | 32 | max files admitted in one `context_pack` |
| `ASK_FABLE_PACK_MAX_FILE_BYTES` | 1000000 | per-file read cap for `context_pack` (a whole file over this is skipped `too_large`; a line-range is capped on bytes collected) |
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
| `ASK_FABLE_CONTEXT_PATH` | `$XDG_STATE_HOME/ask_fable/context.db` | SQLite store for the context bus (`context_write`/`context_ref`) |
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
| `ASK_FABLE_COUNCIL_TIMEOUT` | `ASK_FABLE_TIMEOUT + 120` | hard upper bound (sec) on `ask_council` wall time — bounds the worst case where a backend swallows its own inner timeout. Oracles that already answered are preserved and synthesized; still-running ones are cancelled and shown as `kind:"timeout"` in `sources`. Only an all-timeout council surfaces `status:"error", kind:"timeout"` |
| `ASK_FABLE_CHAIN_TIMEOUT` | `max(600, n × ASK_FABLE_TIMEOUT)` (min 10) | hard upper bound (sec) on `ask_chain` wall time (the chain is sequential, so the default scales with the number of stages). Surfaces as `status:"error", kind:"timeout"` with the partial `stages[]` collected so far |
| `ASK_FABLE_MAX_PARALLEL` | 6 | semaphore size for council fan-out — bounds simultaneous sockets on the `full` tier so a 12-model fan-out can't exhaust `ulimit -n` |
| `ASK_FABLE_AUDIT_MAX_BYTES` | 52428800 (50 MB) | size cap for the audit log; rotated to `decisions.<timestamp>.<seq>.jsonl` when exceeded |
| `ASK_FABLE_AUDIT_BACKUPS` | unlimited | optional cap on rotated audit segments; set `0` to discard the active segment on rotation |

Default-created persisted state (cache, context bus, hub, audit log, saved answers,
session dumps, and full-trace bundles) is written to a per-user state dir, with
newly created files mode `0600` and parent dirs mode `0700`. SQLite stores
(`cache.db`, `context.db`, `hub.db`) use WAL journal mode for crash safety. Markdown dumps (saved answers,
session transcripts) and the separately located config file go through an atomic
`tempfile + os.replace + fsync` so a crash mid-write can never leave a
partial or empty file on disk.
