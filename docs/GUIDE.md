# ask-fable — setup & usage guide

## Correlated traces

Every MCP tool response includes a schema-v2 `trace_id`, duration, cache and usage
metadata, artifact references, and telemetry health. Use `trace_list` to find recent
calls and `trace_get` to inspect one event timeline. Safe mode is the default and
stores no raw prompt or answer. Set `ASK_FABLE_TRACE_MODE=full` to create redacted,
size-capped trace bundles; use `include_content=true` with `trace_get` to retrieve a
bounded excerpt.

A practical, end-to-end guide: install the MCP server, wire it into Claude Code,
set up each model backend (keys / CLIs / Ollama Cloud / MiniMax), verify it, and
use every tool — including the structured **sidecar** and the **context bus**.

> For a visual, end-to-end map of how the server is wired — the request flow,
> `ask` request pipeline, the oracle bridges, council/chain orchestration, the
> cross-cutting subsystems, and on-disk state — open
> [`architecture.html`](architecture.html) in a browser.

- [1. What you get](#1-what-you-get)
- [2. Prerequisites](#2-prerequisites)
- [3. Install the server](#3-install-the-server)
- [4. Register it in Claude Code](#4-register-it-in-claude-code)
- [5. Set up each backend](#5-set-up-each-backend)
- [6. Verify with `/mcp`](#6-verify-with-mcp)
- [7. How to use it](#7-how-to-use-it)
- [8. Configuration reference](#8-configuration-reference-env-vars)
- [9. Troubleshooting](#9-troubleshooting)

---

## 1. What you get

An MCP server that lets your coding agent consult strong external reasoning models
as a "second brain". Every tool shares one scope guard and returns a JSON result.

| Tool | What it does | Backend |
|------|--------------|---------|
| `ask` | Fable reasoning, **multi-turn** (keeps a `session`) | Claude OAuth (no key) |
| `ask_opus5` | Same as `ask` on Claude Opus 5 — ~half the price, faster; own session namespace | Claude OAuth (no key) |
| `ask_m3` | Ask MiniMax-M3 alone | `mmx` CLI |
| `ask_glm` | Ask GLM-5.2 alone | HTTP + API key |
| `ask_deepseek` | Ask deepseek-v4-pro alone (cheap direct API) | HTTP + API key |
| `ask_gemini` | Ask Gemini 3.1 Pro alone | `agy` CLI |
| `ask_codex` | Ask GPT-5.6 Sol alone | `codex` CLI |
| `ask_grok` | Ask Grok alone | `grok` CLI |
| `ask_kimi` | Ask Kimi alone (kimi-code/k3, 1M ctx) | `kimi` CLI |
| `ask_ollama` | Ask one Ollama Cloud model alone | `ollama` daemon |
| `ask_atlas` / `list_atlas_models` | Ask an Atlas Cloud text model / rank the live catalog and pick a model | HTTP (catalog free; execution key) |
| `ask_openrouter` / `list_openrouter_models` | Ask one of ~400 OpenRouter models / rank the live catalog | HTTP (catalog free; execution key) |
| `ask_council` | Fan out to several models → Fable synthesizes one answer | mix of the above |
| `ask_chain` | Refine an answer through an ordered model pipeline | mix of the above |
| `ask_debate` | Have two models argue, then a third adjudicate (`adjudicator`, default Fable) | mix of the above |
| `ask_ollama_council` | Council of Ollama Cloud models only | `ollama` daemon |
| `ask_atlas_council` | Council of Atlas Cloud models; GPT-5.6 Sol adjudicates (local `codex` CLI preferred, else Atlas-hosted, else Fable) | HTTP + key |
| `ask_openrouter_council` | Cross-lab council of OpenRouter models; same GPT-first adjudicator ladder | HTTP + key |
| `list_ollama_models` | Discover what's on your Ollama Cloud | `ollama` |
| `configure_ollama_council` | Persist your chosen Ollama council | writes config file |
| `configure_atlas_council` | Persist your chosen Atlas council and/or its adjudicator | writes config file |
| `configure_openrouter_council` | Same for `ask_openrouter_council` | writes config file |
| `configure_tracing` | Change safe/full tracing and live reasoning output without restarting | writes config file |
| `context_write` / `context_pack` / `context_read` / `context_list` / `context_delete` | The **context bus**: store or safely pack context once, reference it by key | local SQLite / bounded filesystem reads |
| `reset_session` | Dump + clear a session (`model`: `fable` for `ask`, `opus5` for `ask_opus5`) | — |
| `stats` | Usage/health stats from the audit log (calls, errors, p95 latency per model/provider/tool/session/day; `by="provider"` sees council members one by one, and what the breaker shed) | local JSONL |
| `trace_list` / `trace_get` | Find correlated request traces and inspect a redacted timeline or bounded full-mode excerpt | local JSONL / trace bundles |
| `session_list` / `session_peek` / `session_stats` | Discover shared sessions, preview recent turns, and inspect hub health | local SQLite |

**Fable is the baseline backend.** Every other backend is optional and unavailable
council members are cleanly skipped. The `ask` tool uses Fable when its Claude Code
bridge is installed and authenticated.

---

## 2. Prerequisites

- **Python ≥ 3.11**
- **Claude Code CLI, installed and logged in.** Fable is reached through Claude
  Code's existing OAuth session (`~/.claude/.credentials.json`) — the server sets
  **no** `ANTHROPIC_API_KEY` of its own. If `claude` runs and is signed in, Fable works.
- Optional, only for the extra models: the `mmx`, `agy`, `codex`, `grok`, and/or
  `ollama` CLIs, and API keys for GLM / DeepSeek / Atlas Cloud (see
  [§5](#5-set-up-each-backend)).

---

## 3. Install the server

```bash
# not on PyPI yet — install from source:
git clone https://github.com/baggybin/ask-fable.git && cd ask-fable
pip install -e .                # add ".[dev]" for tests; the salient-core guard is picked up automatically if installed
# or with pipx:
pipx install .
```

This installs the `ask-fable` command (that's the MCP server entry point).

---

## 4. Register it in Claude Code

MCP servers are launched by Claude Code, so **API keys go in the server's `env`
block**, not your shell.

### Option A — edit `~/.claude/.claude.json`

```jsonc
{
  "mcpServers": {
    "ask_fable": {
      "command": "ask-fable",
      "env": {
        // all optional — add only the backends you want:
        "ASK_FABLE_GLM_API_KEY": "sk-...",
        "ASK_FABLE_DEEPSEEK_API_KEY": "sk-..."
      }
    }
  }
}
```

(If that file is root-owned, edit it as its owner, e.g. via `sudo`.) Use
`"command": "python3", "args": ["-m", "ask_fable"]` if you didn't install the
console script. **Restart Claude Code** after editing.

### Option B — the `claude mcp` CLI

```bash
claude mcp add ask_fable -- ask-fable
# with an API key in the server env:
claude mcp add ask_fable -e ASK_FABLE_GLM_API_KEY=sk-... -- ask-fable
claude mcp list          # confirm it's registered
```

Once registered the tools appear as `mcp__ask_fable__ask`,
`mcp__ask_fable__ask_council`, etc.

---

## 5. Set up each backend

You only need the ones you'll use. Order of effort: **Fable (nothing)** →
**Ollama Cloud (one signin)** → **MiniMax/Gemini/Codex/Grok (a CLI login)** →
**GLM/DeepSeek/Atlas/OpenRouter (an API key when applicable)**.

### Fable (required, zero config)
Nothing to do beyond a logged-in Claude Code. Verify: `claude -p "hi"` returns text.

### Ollama Cloud (for `ask_ollama`, `ask_ollama_council`, the `full` tier)
The default transport is your **local Ollama daemon**, which proxies `:cloud`
models through your Ollama sign-in — so **no API key** is stored by the server.

```bash
# install ollama (see ollama.com), then:
ollama serve          # run the daemon (background service on most installs)
ollama signin         # sign in so :cloud models are reachable
```

That's it — `ask_ollama` now works. Default model: `gpt-oss:120b-cloud`.

**Remote instead of local daemon** (optional): point at ollama.com directly:

```
ASK_FABLE_OLLAMA_BASE_URL=https://ollama.com
ASK_FABLE_OLLAMA_API_KEY=<your ollama cloud key>
```

**Pick your council** (persists across sessions) — let the agent do it for you:
> "Configure the ask_fable Ollama council."

It calls `list_ollama_models` to show what's live on your cloud (GLM, MiniMax-M3,
Qwen, Kimi, DeepSeek, Nemotron, gpt-oss, …), asks which you want, and saves them
with `configure_ollama_council` to `~/.config/ask_fable/config.json`.

### MiniMax (for `ask_m3` and the default council)
Uses the **`mmx` CLI**, already API-key authenticated on the host — the server
sets no key.

```bash
# install the mmx CLI per MiniMax's instructions, then:
mmx auth login
mmx text chat --model MiniMax-M3 --messages-file - <<<'[{"role":"user","content":"hi"}]'   # smoke test
```

Default model `MiniMax-M3`; override with `ASK_FABLE_MINIMAX_MODEL`.

### Gemini (for `ask_gemini`)
Uses the **`agy` CLI** (fronts Google's Gemini), already signed in on the host.

```bash
# install `agy` and sign in per its instructions, then:
agy -p "hi"           # smoke test — should print an answer
```

Default model `Gemini 3.1 Pro (High)`; override with `ASK_FABLE_GEMINI_MODEL`.

### Codex (for `ask_codex`)
Uses the already-authenticated **`codex` CLI**; the server runs `codex exec` in a
hermetic, read-only mode, so put the code it needs in `context`.

```bash
codex login
codex exec --sandbox read-only "Answer briefly: what is 2 + 2?"  # smoke test
```

Default model `gpt-5.6-sol`; override it with `ASK_FABLE_CODEX_MODEL`, reasoning
effort with `ASK_FABLE_CODEX_REASONING`, and its timeout with
`ASK_FABLE_CODEX_TIMEOUT`.

### Grok (for `ask_grok` and local `xai/grok-*` Atlas routes)
Uses the already-authenticated **`grok` CLI**; it does not require an Atlas API
key when the local CLI route is available. Verify the installed CLI can answer a
simple non-interactive prompt, then use `ask_grok`. Default model is `grok-4.6`;
`ASK_FABLE_GROK_MODEL`, `ASK_FABLE_GROK_REASONING`, and
`ASK_FABLE_GROK_TIMEOUT` tune it. The default low reasoning effort keeps
context-heavy turns bounded.

### Kimi (for `ask_kimi` and local `moonshotai/kimi-*` Atlas routes)
Uses the already-authenticated **`kimi` CLI** (Kimi Code); no Atlas API key is
needed on this route. Default model is `kimi-code/k3`.
`ASK_FABLE_KIMI_MODEL`, `ASK_FABLE_KIMI_EFFORT` (`low`/`high`/`max`, and the
`quick`/`standard`/`deep` presets), and `ASK_FABLE_KIMI_TIMEOUT` tune it.

**Context ceiling.** k3 is a 1M-context *model*, but this *transport* is not:
the CLI accepts the prompt only as an argv value, which Linux caps at
`MAX_ARG_STRLEN` (~131k bytes). Prompts above ~120k are refused up front with a
`context_too_large` error pointing at `ask_atlas` with `moonshotai/kimi-k3`,
which is HTTP and has no such limit.

Unlike `grok`, this CLI exposes no flags for restricting tools or overriding the
system prompt, and its `-p` mode is a fully agentic loop that will walk your
filesystem if allowed to. Each turn therefore runs against a generated
`KIMI_CODE_HOME` under `${XDG_STATE_HOME:-~/.local/state}/ask_fable/kimi-home/`
which reuses your login but grants no workspace trust and no tools, so the model
answers only from `context`. A turn that manages to run a tool anyway is
discarded as an error rather than returned. Set `ASK_FABLE_KIMI_HOME` if your
real Kimi home is not `~/.kimi-code`.

### GLM (for `ask_glm`) and DeepSeek (for `ask_deepseek`) — API keys
These speak the Anthropic `/v1/messages` shape over HTTP. Set the key(s) in the
server `env`:

```
ASK_FABLE_GLM_API_KEY=...          # base_url defaults to https://api.z.ai/api/anthropic, model glm-5.2
ASK_FABLE_DEEPSEEK_API_KEY=...     # base_url defaults to https://api.deepseek.com/anthropic, model deepseek-v4-pro
```

`ask_glm` degrades gracefully: with no `ASK_FABLE_GLM_API_KEY` it is served by
Atlas-hosted `zai-org/glm-5.3` on the Atlas key, and only reports
`not_configured` when neither key is present. The direct Z.ai endpoint is
cheaper, so it always wins when its key is set. Note that these two routes speak
different protocols — Z.ai is Anthropic `/v1/messages`, Atlas is OpenAI-compatible
`/v1/chat/completions` — so pointing `ASK_FABLE_GLM_BASE_URL` at Atlas does *not*
work; the fallback is what bridges them.

Override per provider with `ASK_FABLE_<GLM|DEEPSEEK>_BASE_URL` /
`ASK_FABLE_<GLM|DEEPSEEK>_MODEL`. Unconfigured providers are reported as
`not_configured` and skipped — never fatal.

### Atlas Cloud (for `ask_atlas` and dynamic `atlas:<model-id>` tokens)
`list_atlas_models` reads Atlas's live catalog without credentials. Atlas HTTP
calls need `ASK_FABLE_ATLAS_API_KEY` or the compatible `ATLASCLOUD_API_KEY`
in the server environment. Set `ASK_FABLE_ATLAS_MODEL` to override the default
model and `ASK_FABLE_ATLAS_BASE_URL` only for a trusted compatible endpoint,
because it receives the bearer key and request content. Requests
for `xai/grok-*` use the authenticated local `grok` CLI when it is installed,
so they do not need an Atlas API key.

### OpenRouter (for `ask_openrouter` and `openrouter:<model-id>` tokens)
One API key reaches ~400 models from every major lab. `list_openrouter_models`
reads the live catalog without credentials. Execution calls need
`ASK_FABLE_OPENROUTER_API_KEY` or the conventional `OPENROUTER_API_KEY` in the
server environment. `ASK_FABLE_OPENROUTER_MODEL` overrides the default model
(`deepseek/deepseek-v4-pro`); set `ASK_FABLE_OPENROUTER_BASE_URL` only for a
trusted compatible endpoint, because it receives the bearer key and request
content. Grok and Kimi model ids reroute to the authenticated local
`grok`/`kimi` CLIs when installed, so those need no OpenRouter key. The
council defaults (`ASK_FABLE_OPENROUTER_COUNCIL` / `_SYNTHESIZER` / `_EFFORT`)
can also be persisted with `configure_openrouter_council`.

---

## 6. Verify with `/mcp`

In Claude Code, run **`/mcp`** — it lists MCP servers, their connection status,
and their tools; you can (re)authenticate OAuth servers there. You should see
`ask_fable` connected with its tools. From the shell: `claude mcp list` and
`claude mcp get ask_fable`.

Quick functional check — just ask your agent:
> "Use ask_fable to sanity-check this plan: …"

---

## 7. How to use it

You rarely call these by hand — the server ships instructions that make the agent
reach for them. But here's the model.

### `ask` — your default second opinion (multi-turn)
```jsonc
ask({
  "question": "Should I memoize handle() with lru_cache or a dict? Pick one.",
  "context": "def handle(key): return _expensive(key)  # pure, hot path",
  "session": "cache-decision"     // reuse the key for follow-ups; Fable keeps context
})
```
Put the **real code in `context`** — the model can't see your repo, so a bare
question with no context is weak. Frame **one specific decision**.

### The response **sidecar**
Every answer is clean prose **plus** a machine-actionable summary:
```jsonc
{
  "status": "ok",
  "answer": "Use lru_cache — …",       // human-readable, block stripped out
  "sidecar": {
    "recommendation": "apply",          // apply | investigate | reject | needs_more_context
    "confidence": "high",               // low | medium | high
    "needs_context": ["downstream.fetch signature"]   // what to paste to get a better answer
  },
  "missing_sidecar": false
}
```
Act on `sidecar.recommendation`. When the model needs more, the result also carries
a **`followup`**: `{"needs_context":[...], "how":"paste these … and re-ask on session
'…'", "likely_already_pasted":[...]}` — paste exactly what it names (or `context_write`
them and pass `context_ref`) and re-ask on the same `session`, but check
`likely_already_pasted` and re-read your own paste first rather than resending it. If
the model still can't answer after repeated tries the turn returns
`status:"context_exhausted"` (with the best-effort answer) so a re-ask loop can't spin
forever — tune the cap with `ASK_FABLE_MAX_NEEDS_CONTEXT` (default 2).

### The **context bus** — paste once, reference by key
Kills the re-paste tax when the same code feeds many asks (and lets sibling agents
share it — the store is shared by every agent on the server):

```jsonc
context_write({ "key": "repo:auth", "value": "<the whole auth module>", "description": "auth" })

ask({ "question": "Where can a token be replayed?", "context_ref": "repo:auth" })   // no re-paste
ask({ "question": "Is the refresh path safe?",       "context_ref": ["repo:auth","repo:session"] })

context_list({})     // discover what's already stored before you paste again
context_read({ "key": "repo:auth" })
context_delete({ "key": "repo:auth" })
```
`context_ref` works on **every** ask tool and all four councils (councils share one
blob across all panelists). A **missing** key is reported, not fatal — unless
**every** ref is missing **and** there's no other context, in which case you get
`status:"needs_context"` with a `did_you_mean` suggestion instead of a blind answer.

### Single-model tools
`ask_m3` / `ask_glm` / `ask_deepseek` / `ask_gemini` / `ask_codex` / `ask_grok` / `ask_kimi` /
`ask_ollama` / `ask_atlas` share the `question` / `context` / `context_ref`
shape and are single-turn (cached for ~1h). `ask_ollama` takes an optional
`model` (for example `"kimi-k2.7-code:cloud"); `ask_atlas` takes an optional
Atlas `model` and `effort` (`quick`, `standard`, or `deep`).

### Atlas recommendation and selection
Ask for the best Atlas model for a concrete job and the agent should call:

```jsonc
list_atlas_models({
  "task": "debug a large Rust repository",
  "limit": 5,           // 2–8, default 5
  "interactive": true   // request a native picker when the client supports it
})
```

The free catalog call ranks a provider-diverse shortlist from live capability
profiles, tags, context length, latency, and pricing. It is guidance from
metadata, not an independent benchmark. An MCP client that supports form
elicitation receives a native model-and-effort form; its accepted choice appears as
`selection: {action:"accept", model, effort}`. Other clients receive the same
options under `picker` for the host to render. `refresh:false` deliberately
does no network request and returns only the effort choices.

Call `ask_atlas` with the accepted or rendered selection. Atlas models can also
appear in explicit `models`, chains, and debates as `atlas:<model-id>` tokens,
for example `models:["fable", "atlas:deepseek-ai/deepseek-v4-pro"]`.

### Councils — for hard, hard-to-reverse decisions
Fan out to several models in parallel, then Fable reconciles them into one answer;
each raw answer is also returned under `sources`.

```jsonc
ask_council({ "question": "…architecture decision…", "context": "…",
              "models": ["fable","minimax","glm","gemini","deepseek"] })
// or a named tier instead of `models`:
ask_council({ "question": "…", "tier": "default" })   // default = fable+minimax (+deepseek when its key is set)
```
Tiers: **`default`** (fable+minimax, +deepseek when `ASK_FABLE_DEEPSEEK_API_KEY`
is configured — cheap direct models are preferred and consulted first) ·
**`middle`** (+opus+glm+gemini+codex+grok+kimi, cheap-first order) ·
**`full`** (+your configured Ollama council). `ask_ollama_council` is the
Ollama-only variant.

**Read the envelope**: `quorum` (e.g. `"2/5"`), `degraded`, `confidence`,
`recommended_next_action`, and `consensus` (`strong` | `partial` | `divergent` |
`unknown`) + `material_disagreement` — computed from the panelists'
recommendations, with each `sources` entry showing that model's `recommendation`
so you can see *who* endorsed what. When the panel is materially split, the
synthesizer is told to pick a side and justify, not average. Panel answers are
anonymized to the synthesizer to blunt self-preference bias. A `1/N` answer is one
opinion, not consensus. Councils are slower and heavier — use `ask` for routine
questions, at most one council per problem.

**Council timeouts**: every council fan-out is bounded by `ASK_FABLE_COUNCIL_TIMEOUT`
(default `ASK_FABLE_TIMEOUT + 120` sec) and a `Semaphore(ASK_FABLE_MAX_PARALLEL)` (default
6 simultaneous calls) so a hung backend or the `full` tier can't exhaust `ulimit -n`.
On timeout the oracles that already answered are **preserved** and synthesized as usual;
the ones still running are cancelled and reported per-oracle as `kind:"timeout"` in
`sources`. Only when *no* oracle finished in time does the whole call surface
`status:"error", kind:"timeout"` — no silent stuck calls.

**Circuit breaker**: a backend whose recent error rate crosses
`ASK_FABLE_BREAKER_THRESHOLD` (default 0.5 over the last `ASK_FABLE_BREAKER_WINDOW=20`
outcomes, min 5 samples) is auto-skipped as `circuit_open` in council/chain fan-out —
exactly like `not_configured`, so the council degrades instead of stalling. Cached
answers are still served. After `ASK_FABLE_BREAKER_COOLDOWN` (default 300s) one probe
is allowed; success closes the breaker. Refusals and config states (`not_configured`)
never trip it. Disable with `ASK_FABLE_CIRCUIT_BREAKER=0`. Every trip, failed probe
and recovery prints one `⚠ circuit breaker …` line to stderr and lands as a
`breaker.opened` / `breaker.reopened` / `breaker.closed` event in the trace log
(`trace_list(provider=…)` finds it); calls it shed show up in `stats` as
`circuit_open`, not as errors — and still sort by traffic, so an oracle shed for
a whole window does not sink below the healthy ones. In a member's own
`provider.completed` event the error `kind` is `cancelled` rather than `timeout`:
that is what the oracle saw, since it cannot know whether a council cap, a chain
cap or a client disconnect stopped it — the orchestrator's `sources` still says
`timeout`.

### Sessions
`ask` holds a `session`; reuse the key to think across turns without restating.
Start fresh with a new key or `reset:true`; `reset_session` dumps + clears one.

### Cross-instance session hub

The hub lets local MCP instances see completed work from other agents. It is a
visibility-only mirror: successful turns that pass the tool-level cache are
written **after** an oracle result is available (which can still be an underlying
per-oracle cache result), and hub data is never injected into an ask, council,
chain, or debate. It does not resume the per-process `ask` session. An agent may
still explicitly copy a discovered turn into a later prompt.

Use the same `session` label for agents collaborating on one decision, then call:

```jsonc
session_list({})                                  // current project, live sessions only
session_peek({ "session_key": "db-migration" }) // full retained Q/A across agents
session_stats({ "window_s": 0 })                 // all retained totals by oracle/agent/status
```

`session_list` defaults to the current project, a 50-row limit, and active-only
results (five-minute heartbeat freshness). `all_projects:true` includes every
project in the local database; `active_only:false` includes stale history.
`session_peek` is intentionally not project-scoped: it returns all matching
labels unless you pass `agent_id`. It contains full questions and answers (not the
separately supplied `context` field), so use unique labels for sensitive work and
only inspect data you are permitted to read.
`session_stats` is project-scoped by default and uses the last 24 hours;
`window_s:0` includes all retained turn history.

The default WAL database is `${XDG_STATE_HOME:-~/.local/state}/ask_fable/hub.db`.
A newly created file is requested with mode `0600` in a per-user state directory;
existing or custom paths retain their own permissions. Set `ASK_FABLE_HUB=0` to
disable future hub reads/writes (it does not erase existing rows); use
`ASK_FABLE_HUB_PATH` only for storage trusted by every reader. Multi-oracle
`session` values (`ask_council`, `ask_chain`, `ask_debate`, `ask_ollama_council`)
are hub grouping keys rather than Fable resume keys and default to the tool name.

### Scope / the guard
Software-engineering questions, including conceptual/brainstorming ones with no
code context (broad is fine — architecture, refactors, tooling, ideation, ideas
for future code). **Refused** only when the question itself directly asks for
offensive-security work (exploit development, attack tooling) or non-software
domain knowledge (e.g. biology); questions about security-related code are
normal engineering, and the guard scans the question, not the context. Note the
guard whole-word-blocks a few security terms — including
the everyday word **"payload"**; say "request/response body" instead.

---

## 8. Configuration reference (env vars)

All optional; set in the server's `env` block. Defaults in parentheses.

**Backends**
| Var | Purpose |
|-----|---------|
| `ASK_FABLE_GLM_API_KEY` / `_BASE_URL` / `_MODEL` | GLM (`https://api.z.ai/api/anthropic`, `glm-5.2`). Unset -> `ask_glm` falls back to Atlas-hosted `zai-org/glm-5.3` |
| `ASK_FABLE_DEEPSEEK_API_KEY` / `_BASE_URL` / `_MODEL` | DeepSeek (`https://api.deepseek.com/anthropic`, `deepseek-v4-pro`) |
| `ASK_FABLE_ATLAS_API_KEY` / `ATLASCLOUD_API_KEY` | Atlas Cloud key for HTTP `ask_atlas` and `atlas:<model-id>` calls; local `xai/grok-*` routes use the authenticated `grok` CLI |
| `ASK_FABLE_ATLAS_BASE_URL` / `_MODEL` | Atlas endpoint (`https://api.atlascloud.ai`) / default model (`xai/grok-4.6`) |
| `ASK_FABLE_OPENROUTER_API_KEY` / `OPENROUTER_API_KEY` | OpenRouter key for `ask_openrouter`, `ask_openrouter_council` and `openrouter:<model-id>` tokens (either name accepted; catalog needs no key) |
| `ASK_FABLE_OPENROUTER_BASE_URL` / `_MODEL` | OpenRouter endpoint (`https://openrouter.ai/api/v1`) / default model (`deepseek/deepseek-v4-pro`) |
| `ASK_FABLE_OPENROUTER_COUNCIL` / `_SYNTHESIZER` / `_EFFORT` | Default panel, adjudicator and effort for `ask_openrouter_council` (config file keys win) |
| `ASK_FABLE_MINIMAX_MODEL` | MiniMax model (`MiniMax-M3`); auth via `mmx` CLI |
| `ASK_FABLE_GEMINI_MODEL` | Gemini model (`Gemini 3.1 Pro (High)`); auth via `agy` CLI |
| `ASK_FABLE_OLLAMA_BASE_URL` | Ollama endpoint (`http://localhost:11434`) |
| `ASK_FABLE_OLLAMA_API_KEY` | Ollama Cloud key (only for a **remote** endpoint) |
| `ASK_FABLE_OLLAMA_MODEL` | `ask_ollama` default model (`gpt-oss:120b-cloud`) |
| `ASK_FABLE_OLLAMA_COUNCIL` | Comma/space list of Ollama council models |

**Behavior**
| Var | Purpose |
|-----|---------|
| `ASK_FABLE_TIMEOUT` | Per-model timeout seconds (`240`) |
| `ASK_FABLE_COUNCIL_TIMEOUT` | Hard cap on `ask_council` wall time (`ASK_FABLE_TIMEOUT + 120`) |
| `ASK_FABLE_CHAIN_TIMEOUT` | Hard cap on `ask_chain` wall time (`max(600, n × ASK_FABLE_TIMEOUT)`, min 10) |
| `ASK_FABLE_MAX_PARALLEL` | Semaphore size for council fan-out (`6`) |
| `ASK_FABLE_MAX_TOKENS` | Output token cap (`65536`) |
| `ASK_FABLE_USE_CLI` | Reach Fable via the `claude` CLI instead of the SDK (`0`) |
| `ASK_FABLE_CACHE` / `_CACHE_TTL` / `_CACHE_PATH` | Answer cache for single-shot tools (on, `3600`s, WAL) |
| `ASK_FABLE_SAVE` / `_OUTPUT_DIR` | Explicit `1` persists each answer to Markdown; unset saves only in full trace mode (atomic rename) |
| `ASK_FABLE_MAX_ANSWERS` / `_MAX_SESSIONS` | Retention caps for saved answers / session dumps (`0` = unlimited). Only ask-fable's own files are pruned, but the default dirs are shared per user — one agent's cap prunes the shared archive |
| `ASK_FABLE_CONTEXT_PATH` | Context-bus SQLite file (WAL) |
| `ASK_FABLE_HUB` / `_HUB_PATH` | Enable the cross-instance hub (on) / SQLite path (`$XDG_STATE_HOME/ask_fable/hub.db`) |
| `ASK_FABLE_HUB_MAX_ROWS` / `_HUB_STALE_SECONDS` / `_HUB_PREVIEW_CHARS` | Retention cap (`10000`) / active-session threshold (`300`s) / list preview cap (`160` chars) |
| `ASK_FABLE_AGENT_ID` | Explicit hub attribution label; otherwise the MCP client identity is inferred |
| `ASK_FABLE_CLI_MAX_PARALLEL` | Per-binary local-CLI concurrency gate for `claude`/`mmx`/`grok`/`codex`/`agy`/`kimi` (`2`; `0` disables); queue wait counts against the call's timeout |
| `ASK_FABLE_GROK_MODEL` / `ASK_FABLE_GROK_REASONING` / `ASK_FABLE_GROK_TIMEOUT` | Grok CLI settings (`grok-4.6` / `low` / global timeout fallback) |
| `ASK_FABLE_KIMI_MODEL` / `ASK_FABLE_KIMI_EFFORT` / `ASK_FABLE_KIMI_TIMEOUT` / `ASK_FABLE_KIMI_HOME` | Kimi CLI settings (`kimi-code/k3` / `high` / global timeout fallback / `~/.kimi-code`) |
| `ASK_FABLE_MAX_NEEDS_CONTEXT` | Consecutive `needs_more_context` turns before `context_exhausted` (`2`) |
| `ASK_FABLE_CONFIG_FILE` | Config file for the persisted Ollama council (atomic-rename) |
| `ASK_FABLE_MIN_LEN` / `_MAX_LEN` / `_MAX_CONTEXT_LEN` | Guard size floors/caps (`3` / `65536` / `0`=unbounded, floored 512k) |
| `ASK_FABLE_DENYLIST_FILE` | Extra prohibited-term list (one per line, hot-path memoized on mtime) |
| `ASK_FABLE_AUDIT_RAW` | Store raw (not hashed) questions in the audit log (`0`) |
| `ASK_FABLE_AUDIT_RAW_CONTEXT` | Split switch for raw context — `AUDIT_RAW=1` + this `0` keeps questions raw but context hashed-only (default: follows `AUDIT_RAW`) |
| `ASK_FABLE_AUDIT_MAX_BYTES` / `_AUDIT_BACKUPS` | Audit log size cap (`50 MB`) and optional retained-rotation cap (unlimited by default) |
| `ASK_FABLE_QUIET` | Silence the console reporter (`0`) |

Default-created state lives under `${XDG_STATE_HOME:-~/.local/state}/ask_fable/`
(answers, cache, context, hub, sessions, audit log, full-trace bundles; newly
created files request mode `0600`). The SQLite stores (`cache.db`, `context.db`,
`hub.db`) use WAL journal mode for crash safety. Markdown outputs/transcripts and config under
`${XDG_CONFIG_HOME:-~/.config}/ask_fable/config.json` use `tempfile + os.replace
+ fsync` so a crash mid-write cannot leave a partial or empty file on disk.

---

## 9. Troubleshooting

- **`ask_fable` not listed in `/mcp`** — restart Claude Code after editing
  `.claude.json`; check `claude mcp get ask_fable`; confirm `ask-fable` is on PATH
  (`which ask-fable`).
- **Fable errors / `binary_missing`** — Claude Code isn't logged in; run a bare
  `claude -p "hi"`. Set `ASK_FABLE_USE_CLI=1` if the SDK path misbehaves.
- **`not_configured` for GLM/DeepSeek** — the API key isn't in the server `env`
  (restart after adding). Remember: keys go in the MCP `env` block, not your shell.
- **MiniMax/Gemini `binary_missing`** — `mmx` / `agy` not on PATH or not signed in
  (`mmx auth login`; `agy -p "hi"`).
- **Ollama skipped** — daemon not running or not signed in (`ollama serve` +
  `ollama signin`), or a remote endpoint without `ASK_FABLE_OLLAMA_API_KEY`. Use
  `list_ollama_models` to see what's reachable.
- **A question is "refused: offensive-security content"** — reword; avoid blocked
  security terms, notably **"payload"** → "request/response body".
- **Council says `degraded` / low `quorum`** — some members were unconfigured or
  timed out; widen `models`, configure the missing backends, or raise `ASK_FABLE_TIMEOUT`.
