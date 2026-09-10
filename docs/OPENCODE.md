# ask-fable with opencode

This is the **opencode-specific** companion to [`GUIDE.md`](GUIDE.md). It covers
installing `ask-fable`, registering it as an MCP server in
[opencode](https://opencode.ai), and the small differences from the Claude Code
setup. The tool set, guard, context bus, and backends are identical across both
clients — only the registration shape and a few environment details differ.

> The full tool reference, backend setup, and env-var tables live in
> [`GUIDE.md`](GUIDE.md) and the root [`README.md`](../README.md). Read this
> doc for the opencode wiring; read those for everything else.

---

## 1. Prerequisites

- **Python ≥ 3.11**
- **opencode** installed.
- **Claude Code CLI, installed and logged in.** The `ask` tool reaches Fable
  (the newest `claude-fable-*`) through Claude Code's existing OAuth session — `ask-fable`
  sets no `ANTHROPIC_API_KEY` of its own. If `claude` runs and is signed in,
  `ask` works. (This dependency is the same as for Claude Code.)
- Optional, only for the extra model backends: the `mmx`, `agy`, `codex`, `grok`,
  and/or `ollama` CLIs, plus API keys for GLM / DeepSeek / Atlas Cloud. See
  [§5](#5-optional-backends).

---

## 2. Install the server

```bash
# not on PyPI yet — install from source:
git clone <repo> && cd ask-fable
pip install -e .
# or with pipx:
pipx install .
```

Verify the binary is on your PATH:

```bash
ask-fable --transport stdio --help
which ask-fable
```

---

## 3. Register it in opencode

opencode discovers MCP servers from its config file. Edit your **global**
config at `~/.config/opencode/opencode.json` (or a project-local
`opencode.json` / `.opencode/opencode.json`), and add an entry under `mcp`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "ask_fable": {
      "type": "local",
      "command": ["ask-fable"],
      "enabled": true
    }
  }
}
```

That minimal block is all `ask` (Fable) needs.

### Field reference (opencode local MCP)

These are the schema-valid fields for a `"type": "local"` server, validated
against `https://opencode.ai/config.json`:

| Field | Required | Type | Notes |
|-------|----------|------|-------|
| `type` | yes | `"local"` | discriminator |
| `command` | yes | array of strings | the command + args, e.g. `["ask-fable"]` |
| `enabled` | no | boolean | enable/disable on startup |
| `environment` | no | object `{VAR: "value"}` | env vars passed to the server process |
| `cwd` | no | string | working directory for the process |
| `timeout` | no | integer (ms) | per-request timeout; default 5000 |

> **The env field is `environment`, not `env`.** opencode's local-MCP schema
> names it `environment` (a remote-MCP server has no env field at all). Writing
> `env` silently passes nothing through, so your API keys never reach the
> server. opencode validates config strictly and refuses to start on unknown
> top-level keys, but unknown keys *inside* an MCP block can slip through — so
> get this name right.

### Adding optional API keys (GLM, DeepSeek)

To enable the `glm` and `deepseek` council oracles plus the `ask_glm` and
`ask_deepseek` single tools, put their keys in `environment` (the deepseek key
also grows the default council to fable+deepseek+minimax, cheap-first):

```json
{
  "mcp": {
    "ask_fable": {
      "type": "local",
      "command": ["ask-fable"],
      "enabled": true,
      "environment": {
        "ASK_FABLE_GLM_API_KEY": "<your z.ai key>",
        "ASK_FABLE_DEEPSEEK_API_KEY": "<your deepseek key>"
      }
    }
  }
}
```

Every other `ASK_FABLE_*` env var from the
[configuration reference](../README.md#configuration-reference) works here too —
for example `ASK_FABLE_TRACE_MODE`, `ASK_FABLE_GLM_MODEL`,
`ASK_FABLE_PROJECT_ROOT` (required if you want `context_pack` to be able to read
files), etc. Keys never go in the repo; keep them in this config file.

---

## 4. Restart opencode

opencode loads MCP servers **once at startup** and does not hot-reload config.
After saving `opencode.json`, **quit and restart opencode**. On every subsequent
launch the server auto-connects — you only register it once.

You can smoke-test the server by hand before launching opencode (it speaks JSON
over stdio):

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' \
  | ask-fable --transport stdio
```

A response containing `"serverInfo":{"name":"ask_fable",...}` means it's healthy.

---

## 5. Optional backends

`ask` (Fable) and its sibling `ask_opus5` (Claude Opus 5, same OAuth session) are
the only tools that work out of the box. Every other backend is
opt-in and is cleanly skipped (reported as `not_configured` / `binary_missing` /
`circuit_open`) if absent — never fatal to a council. Set them up only for the
models you want:

| Backend / tool | What it needs | One-time setup |
|----------------|---------------|----------------|
| `ask_m3` (MiniMax-M3) | `mmx` CLI | install `mmx`, then `mmx auth login` |
| `ask_gemini` (Gemini 3.1 Pro) | `agy` CLI | install `agy`, then sign in once |
| `ask_codex` (GPT-5.6 Sol) | `codex` CLI | install `codex`, then `codex login` |
| `ask_grok` (Grok 4.6) | `grok` CLI | install `grok`, then sign in once |
| `ask_kimi` (kimi-code/k3) | `kimi` CLI | install `kimi` (Kimi Code), then `kimi login` |
| `ask_ollama` / `ask_ollama_council` | `ollama` daemon | `ollama serve`, then `ollama signin` for `:cloud` models |
| `ask_glm` (GLM-5.2) | Z.ai API key | `ASK_FABLE_GLM_API_KEY` in `environment` |
| `ask_deepseek` (deepseek-v4-pro) | DeepSeek API key | `ASK_FABLE_DEEPSEEK_API_KEY` in `environment` |
| `ask_atlas` / `atlas:<model-id>` | Atlas HTTP key or local Grok CLI | Set `ASK_FABLE_ATLAS_API_KEY` or `ATLASCLOUD_API_KEY` for HTTP; `xai/grok-*` uses authenticated local `grok` when available |
| `ask_openrouter` / `ask_openrouter_council` | OpenRouter API key | `ASK_FABLE_OPENROUTER_API_KEY` (or `OPENROUTER_API_KEY`) in `environment`; the catalog lookup needs no key |

The CLIs reuse their own authenticated sessions, exactly as the Fable bridge
reuses Claude Code's OAuth — `ask-fable` sets none of those keys itself. Full
backend detail is in [`GUIDE.md` §5](GUIDE.md).

---

## 6. How the tools surface in opencode

opencode prefixes MCP tool names with the server name, so the 37 tools arrive as
`mcp__ask_fable__ask`, `mcp__ask_fable__ask_council`, `mcp__ask_fable__ask_m3`,
`mcp__ask_fable__context_write`, and so on. Your agent calls them like any other
tool.

The injected standing instruction (the "use `ask` liberally and early" guidance)
is delivered to the agent the same way as in Claude Code. For best results with
weaker local models that under-attend to system prompts, also drop a short
decision ladder into your project's `AGENTS.md` (opencode re-reads it each
session). The recommended block is in the root
[`README.md`](../README.md#recommended-agent-instructions); adapt the heading to
suit your project.

---

## 7. Using it

The contract is identical to Claude Code — only the call site differs. From the
agent's perspective:

- **`ask`** for one focused question with the real code/error pasted into
  `context`. Reuse the `session` key for follow-ups (Fable keeps context
  server-side).
- **`ask_council`** only for a contentious, hard-to-reverse decision
  (architecture, concurrency, data model, public API). Check the `quorum` /
  `degraded` / `consensus` fields — a 1-of-N answer is one opinion, not
  agreement.
- **`ask_chain`** when you want *ordered* refinement (e.g. a cheap model drafts,
  Fable finalizes) instead of a parallel vote.
- **`ask_debate`** to stress-test a high-stakes call by pitting two models
  against each other before Fable adjudicates.
- **Atlas selection:** call
  `list_atlas_models({task:"debug a large Rust repository"})` when choosing an
  Atlas model. It ranks live catalog metadata and opens a native model-and-effort
  picker when the client supports form elicitation; otherwise render the returned `picker`.
  Pass an accepted `selection.model` and `selection.effort` to `ask_atlas`.
  `limit` is 2–8 (default 5), and `refresh:false` skips the catalog request.
  Atlas models also work as `atlas:<model-id>` tokens in explicit council,
  chain, and debate model lists.
- **Context bus:** `context_write` a large blob once under a key, then pass
  `context_ref=<key>` on any ask tool instead of re-pasting. To pack repo files
  by path, set `ASK_FABLE_PROJECT_ROOT` and use `context_pack`.
- **Cross-instance hub:** use a shared `session` label when local agents are
  coordinating, then call `session_list({})` and
  `session_peek({session_key:"…"})` to see completed work. This is a local,
  visibility-only mirror: it never becomes oracle context or resumes an `ask`
  session. `session_peek` returns full retained Q/A across matching labels, so
  treat it as sensitive local data; see the [README hub reference](../README.md#cross-instance-session-hub).

Every tool returns one JSON object: `status`, `answer` (or `sources` for
councils), a machine-readable `sidecar` (`recommendation` / `confidence` /
`needs_context`), a correlated `trace_id`, and `telemetry`. See the
[response contract](../README.md#observability-and-response-shape) for the full
shape, including the `followup` flow and `context_exhausted` terminator.

---

## 8. Troubleshooting

**opencode didn't load the tools.** MCP servers connect at startup — fully quit
and restart opencode after editing `opencode.json`. A running session keeps the
old config.

**opencode fails to start with `ConfigInvalidError`.** Your JSON is malformed or
has an unknown top-level key. Validate it (`python3 -m json.tool
opencode.json`) and confirm the `$schema` line is present. Temporarily skip a
broken project config with `OPENCODE_DISABLE_PROJECT_CONFIG=1` to edit from
inside opencode.

**Optional API keys aren't taking effect.** You probably used `env` instead of
`environment` (see [§3](#3-register-it-in-opencode)). The local-MCP field is
`environment`.

**`ask` returns an error / `ask_glm` or `ask_deepseek` reports `not_configured`.**
`ask` needs the Claude Code CLI signed in (it rides the OAuth session); `ask_glm`
needs `ASK_FABLE_GLM_API_KEY` and `ask_deepseek` needs
`ASK_FABLE_DEEPSEEK_API_KEY` in `environment`. All are reported with a `kind`
field rather than crashing.

**`ask_gemini` / `ask_codex` report `binary_missing`.** The `agy` / `codex` CLI
isn't installed or not on opencode's PATH. Install and authenticate it (see
[§5](#5-optional-backends)).

**`ask_ollama_council` returns every model as `sdk_error`.** The local `ollama`
daemon isn't running. Start it (`ollama serve` or your service manager) and, for
`:cloud` models, run `ollama signin` once.

**A council ran but some members are missing.** Check the result's `quorum`
(`N/M`), `effective_models`, and `degraded`. Unconfigured or tripped-breaker
members appear in `sources` as `not_configured` / `circuit_open`; this is
graceful degradation, not a crash.

**Inspecting what happened.** `trace_list` lists recent calls and `trace_get`
reads one timeline by `trace_id`. `stats` aggregates calls, errors, and latency
per model/session/day from the audit log. These are local, read-only, and make
no model call.

---

## 9. OpenRouter models as native opencode models

**Use `/connect` — do not write a manual provider block.** opencode ships a
native OpenRouter provider, so the whole setup is:

```
/connect        # in the opencode TUI, pick OpenRouter, paste the API key
```

That writes the key to `~/.local/share/opencode/auth.json` and the provider's
~350 models appear in `/models` immediately, keyed as
`openrouter/<vendor>/<model>` (e.g. `openrouter/z-ai/glm-5.3`,
`openrouter/anthropic/claude-sonnet-5`).

**Verified on opencode 1.18.23:** the native provider both lists models and
serves real requests. A hand-written `provider.openrouter` block using
`@ai-sdk/openai-compatible` also works, but it is strictly worse and should be
removed if you have one:

- it duplicates the API key into `opencode.json`, where the native path keeps it
  in the credential store alone;
- a generic openai-compatible block carries only the model ids and names you
  type, losing the per-model cost/context metadata the native provider gets from
  models.dev;
- the curated model list goes stale, while the native provider tracks the live
  catalog.

This is the opposite of the Atlas case below, where there is no native provider
and the manual block IS the reliable path — so don't copy the Atlas recipe for
OpenRouter.

To make OpenRouter the default agent model, set `model` / `small_model` and any
per-agent `model` fields to an `openrouter/...` id.

> Separately from opencode's own picker, the `ask_openrouter` /
> `list_openrouter_models` / `ask_openrouter_council` MCP tools let the *agent*
> call an OpenRouter model mid-conversation. Those read
> `ASK_FABLE_OPENROUTER_API_KEY` from the `mcp.ask_fable.environment` block —
> opencode's `/connect` credential is not visible to the MCP server, so the key
> has to be set in both places if you want both paths.

## 10. Atlas models as native opencode models

The `ask_atlas` / `list_atlas_models` MCP tools let the *agent* call an Atlas
model mid-conversation. If you instead want Atlas models in opencode's own
`/models` picker — driving the agent directly — add a **manual `provider`
block**. Atlas speaks the standard OpenAI `/v1/chat/completions` shape (see
`src/ask_fable/atlas.py`), and opencode bundles the `@ai-sdk/openai-compatible`
adapter, so this needs no ask_fable code change and no extra install.

> **Verified on opencode 1.17.18:** the manual block below lists models AND
> serves real requests (a `grok-4.5` test call returned correctly). The
> first-party `@atlascloudai/opencode` plugin *exists* and would auto-register
> the full catalog, but in testing it failed to load silently on this version
> (no provider registered, no log line) — so the manual block is the reliable
> path. The plugin is noted at the end as an experiment.

### Option A — manual provider block (recommended)

Add a `provider.atlascloud` block to `~/.config/opencode/opencode.json`
(opencode merges configs, so this composes with the `mcp.ask_fable` entry from
[§3](#3-register-it-in-opencode)):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "atlascloud": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Atlas Cloud",
      "options": {
        "baseURL": "https://api.atlascloud.ai/v1",
        "apiKey": "apikey-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "headers": { "User-Agent": "opencode-atlas/1.0 (+https://github.com/baggybin/ask-fable)" }
      },
      "models": {
        "minimaxai/minimax-m3": { "name": "MiniMax M3" },
        "moonshotai/kimi-k2.7-code": { "name": "Kimi K2.7 Code" },
        "zai-org/glm-5.2": { "name": "GLM 5.2" },
        "kwaipilot/kat-coder-pro-v2.5": { "name": "KAT Coder Pro V2.5" },
        "xai/grok-build-0.1": { "name": "Grok Build 0.1" },
        "xai/grok-4.5": { "name": "Grok 4.5" },
        "qwen/qwen3.7-max": { "name": "Qwen3.7 Max" },
        "openai/gpt-5.6-sol": { "name": "GPT 5.6 Sol" },
        "deepseek-ai/deepseek-v4-pro": { "name": "DeepSeek V4 Pro" },
        "anthropic/claude-sonnet-4.6": { "name": "Claude Sonnet 4.6" },
        "kwaipilot/kat-coder-air-v2.5": { "name": "KAT Coder Air V2.5" },
        "deepseek-ai/deepseek-v4-flash": { "name": "DeepSeek V4 Flash" },
        "qwen/qwen3.7-plus": { "name": "Qwen3.7 Plus" },
        "bytedance/doubao-seed-2.0-code-preview-260215": { "name": "Doubao Seed 2.0 Code" },
        "xiaomi/mimo-v2.5-pro": { "name": "MiMo V2.5 Pro" }
      }
    }
  },
  "model": "atlascloud/minimaxai/minimax-m3"
}
```

Then **fully restart opencode** (it loads providers once at startup). `/models`
shows **Atlas Cloud**; select as `atlascloud/<model_id>` (e.g.
`atlascloud/qwen/qwen3.7-max`, `atlascloud/xai/grok-4.5`). The provider ID
splits on the first slash, so the slash-bearing model id passes through verbatim
to the API. Verify from the CLI without opening the TUI:

```bash
opencode models atlascloud                          # should list the models
opencode run -m atlascloud/xai/grok-4.5 "ping"      # should answer
```

Notes on the block:

- **`apiKey`.** Shown inline for simplicity (opencode config is `0600`). Two
  cleaner alternatives: `"{env:ATLASCLOUD_API_KEY}"` if the var is exported in
  opencode's launch shell, or omit it and let opencode resolve a matching
  `atlascloud` entry in `~/.local/share/opencode/auth.json` (`opencode auth
  login` → Atlas Cloud). If `/models` shows the provider but requests 401, the
  key isn't reaching the process — check the env/auth path.
- **`User-Agent` header.** Atlas's edge WAF returns HTTP 403 to some default SDK
  user-agents (ask_fable sets one for the same reason — see `_USER_AGENT` in
  `atlas.py`). opencode's adapter is usually accepted; the header is belt-and-
  suspenders. If you hit 403s, this is the fix.
- **Provider id.** Use `atlascloud` (matches `auth.json`). If you *also* enable
  the plugin below and it loads, it overwrites this block — so don't run both.

### Keeping the list fresh

Run `python3 scripts/atlas_opencode_provider.py` to regenerate the block from
the live catalog. It keeps the curated display names stable and refreshes
ctx/cost as JSONC comments, so the output diffs cleanly when Atlas updates
pricing or context windows:

```
.venv/bin/python scripts/atlas_opencode_provider.py            # print the block
.venv/bin/python scripts/atlas_opencode_provider.py --check    # CI drift check
```

The curated allowlist lives at the top of that script — edit it to change which
models appear. Atlas's catalog endpoint is `/api/v1/models` (note the `/api`
prefix), so opencode's standard `/v1/models` discovery does **not** populate a
manual block — the explicit `models` map is required, which is why the
generator exists.

### The `@atlascloudai/opencode` plugin (experimental)

Atlas Cloud ships a first-party plugin (`@atlascloudai/opencode`) that would
auto-register the *full* catalog at startup by fetching it itself — add
`"@atlascloudai/opencode"` to the `plugin` array and put the key in
`auth.json` under `atlascloud` (`opencode auth login`). It would supersede the
manual block (its config hook overwrites `provider.atlascloud`). **It did not
load in testing on opencode 1.17.18** (no provider registered, no log line),
so treat it as experimental: if a future opencode or plugin version loads it
cleanly, it's the lower-maintenance choice. Until then, use the manual block
above.

### Before you trust a model as the primary agent

opencode streams every response and drives a multi-turn tool loop. **Chat
quality is not the filter — tool-call reliability is.** For each model you plan
to use as the main agent:

1. **UA / endpoint check.** Confirm a 200 (not 403) with the chosen user-agent:
   ```bash
   curl -H 'User-Agent: opencode-atlas/1.0' \
        -H "Authorization: Bearer $ATLASCLOUD_API_KEY" \
        https://api.atlascloud.ai/v1/chat/completions \
        -d '{"model":"<model_id>","messages":[{"role":"user","content":"hi"}]}'
   ```
2. **Streamed tool loop.** Run a real multi-turn task in opencode that uses
   tools. Watch for malformed `tool_calls` deltas, arguments split mid-JSON, or
   a missing `finish_reason: "tool_calls"` — this is the most common failure
   behind OpenAI-compatible gateways.
3. **Reasoning leakage.** Qwen/GLM/Kimi-class models may emit `<think>` tags or
   a nonstandard `reasoning_content` field that leaks into agent output.
4. **Usage in streams.** Confirm opencode's status line shows token usage; some
   gateways omit `usage` on streamed responses, which breaks cost/compaction
   accounting.
5. **`reasoning_effort`.** Unlike ask_fable (which retries without it on a 400),
   opencode will error every request if a model rejects it. Only set
   `reasoningEffort` per-model after confirming the model accepts it.

opencode's own recommended-models list is deliberately small (GPT Codex, Claude
Sonnet/Opus, MiniMax M2.x, Gemini 3 Pro) because *"only a few of them are good
at both generating code and tool calling."* Expect to trim your list based on
step 2 — start with the Tier S models and add from there.
