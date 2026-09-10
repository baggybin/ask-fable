# ask-fable: Multi-Model Reasoning MCP Server

<!-- mcp-name: io.github.baggybin/ask-fable -->
**ask-fable** is a portable, installable **MCP (Model Context Protocol) server** for AI coding agents. It works in **Claude Code**, OpenCode, Kimi Code, Grok, Cursor, Codex, and any other harness that can spawn a local MCP server.

It gives those agents guarded code and architecture reasoning from **Anthropic's Claude Fable** (the newest `claude-fable-*`), Claude Opus 5 (`claude-opus-5`), MiniMax (`MiniMax-M3`), Gemini, Codex, GLM, DeepSeek, Grok, Kimi, and Ollama Cloud models. It can query one backend, synthesize a parallel council, run an ordered refinement chain, or stage a structured adversarial debate.

Fable and Opus 5 use Claude Code's existing OAuth session (through the Agent SDK,
with the `claude` CLI as a fallback). MiniMax, Gemini, Codex, Grok, and local Ollama
similarly reuse authenticated local CLIs. GLM, DeepSeek, and Atlas Cloud are
optional HTTP backends that need server-side API keys.

## Start here

| If you need to… | Use |
|---|---|
| Ask one trusted coding model, with follow-up memory | `ask` (Fable) / `ask_opus5` (Opus 5) |
| Compare independent answers in parallel | `ask_council` |
| Draft, critique, then decide in order | `ask_chain` |
| Stress-test a high-impact decision | `ask_debate` |
| Brainstorm an open question, models arguing to divergence | `ask_conference` |
| Select a task-matched Atlas Cloud model | `list_atlas_models` → `ask_atlas` |
| Atlas council with GPT-5.6 Sol adjudicating | `ask_atlas_council` |
| Reuse large code context without pasting it again | `context_write` + `context_ref` |
| Investigate a request after it ran | `trace_list` + `trace_get` |

**Start with `ask` for one hard question.** Escalate to a council, chain, or
debate only when the decision warrants the extra latency and cost.

## What it gives you

ask-fable gives an MCP client four ways to reason:

| Mode | What happens | Best for |
|---|---|---|
| **Ask** | One model answers directly; Fable can remember a session | Everyday debugging and design questions |
| **Council** | Several models answer in parallel; Fable reconciles them | Comparing independent opinions |
| **Chain** | Models work in order: draft → critique → decide | Deliberate refinement and cost-tiered escalation |
| **Debate** | A proposer and opponent test claims; Fable adjudicates | Contentious, hard-to-reverse decisions |

The same guard, context bus, cache, audit trail, and tracing layer wrap every
mode. Backends are optional: use Fable alone, call a specific provider, or mix
Fable, MiniMax, Gemini, Codex, Grok, GLM, DeepSeek, Ollama, and Atlas Cloud.
Unavailable council members are reported and skipped instead of failing the
whole request.

The cheapest real second opinion is the **`twin`** token — the *twin flames*.
It expands to **both Anthropic reasoners at once, Fable + Claude Opus 5**, and
both ride the same OAuth session as `ask`, so a two-model cross-check costs you
no provider keys and no extra setup:

```python
ask_council(models=["twin"])        # or tier="twin" — the pair, in parallel
ask_chain(pipeline="m3 > twin")     # cheap draft, then fable → opus in turn
```

Five features make the result useful to an agent, not just readable by a human:

- **Structured sidecar** — every answer carries a machine-readable
  `sidecar` (`{recommendation: apply|investigate|reject|needs_more_context,
  confidence, needs_context}`) next to the prose, so an agent acts on it directly.
  When the model needs more, a **`followup`** tells it exactly what to paste, and a
  per-session terminator stops an unbounded re-ask loop (`status:"context_exhausted"`).
- **Context bus** — `context_write` a big codebase context ONCE under a key, then
  pass **`context_ref`** on any ask tool (or council) to pull it in instead of
  re-pasting. Shared by every agent on the server; `context_read` / `context_list` /
  `context_delete` round it out.
- **Council consensus** — councils return a `consensus` signal
  (`strong` | `partial` | `divergent` | `unknown`) + `material_disagreement` computed
  from the panel's recommendations, each `sources` entry shows that model's
  `recommendation`, and the synthesis is **anonymized** (Expert A/B, Fable last) to
  blunt self-preference bias.

- **Correlated traces** — every call includes a `trace_id`; inspect the ordered
  request timeline without storing raw prompts in the default safe mode.
- **Session hub** — successful turns from local MCP instances are mirrored into a
  shared, visibility-only dashboard. Agents can use the same label to coordinate
  work without that shared history ever becoming model context.

## How it works

<p align="center">
  <img src="images/ask-fable-system-map.png" alt="ask-fable system map: an MCP client passes context through the guard and router to single, council, chain, or debate modes backed by multiple model providers">
</p>

A request enters through MCP, resolves any reusable `context_ref`, passes the
guard, and is routed to the chosen reasoning mode. The result is normalized into
an answer plus a machine-readable sidecar, persisted to the configured
observability stores, and returned with a trace ID.

<p align="center">
  <img src="images/ask-fable-request-flow.png" alt="ask-fable request lifecycle: receive, resolve context, guard, cache lookup, run mode, normalize, persist, and return">
</p>

The project ships its own two-layer request gate: a size/sanity floor followed
by a prohibited-use denylist. Fable's model prompt adds the final semantic scope
contract. See [The guard](#the-guard) for the exact behavior.

## The guard
<p align="center">
  <img src="images/guard_layers_modern.jpg" alt="Three-layer guard before any model call: (1) sanity floor, (2) prohibited-use denylist, (3) model scope contract">
</p>

Every question is checked **before any model call**:

1. **Sanity floor** — rejects only empty / too-short (`<3` chars) / too-long
   (`>65536` chars) questions. Context is **unbounded** by default (any cap you set
   is floored to 512,000 chars). Breadth is **allowed**.
2. **Prohibited-use denylist** — ask-fable's bundled offensive-security and
   biology dual-use patterns. Extend it via
   `ASK_FABLE_DENYLIST_FILE` (one term per line). Benign multi-word phrases
   (e.g. `request payload`) are neutralized *before* matching so an ambiguous
   word like `payload` used in an ordinary engineering sense doesn't false-trip;
   add your own via `ASK_FABLE_ALLOWLIST_FILE` (one phrase per line). This only
   rescues the exact benign phrase — a bare prohibited term still rejects.
3. **Model scope contract** — Fable answers engineering questions, including
   conceptual/brainstorming ones with no code context (breadth is fine), and
   replies `REFUSED: <reason>` only when the question itself directly asks for
   offensive-security work (exploit development, attack tooling) or non-software
   domain knowledge (e.g. biology). Questions about security-related code are
   normal engineering.

Every decision is appended to an owner-only JSONL audit log (question hashed by
default; `ASK_FABLE_AUDIT_RAW=1` to store raw).

## Quick start

### 1. Install

> **New here?** The [setup & usage guide](docs/GUIDE.md) walks through install,
> registering in Claude Code (OpenCode, Kimi Code, Grok, and other MCP clients
> use the same server — see [below](#2-register-in-your-coding-harness)),
> setting up every backend (API keys, Ollama Cloud, MiniMax/Gemini CLIs),
> `/mcp` verification, and how to use every tool.
>
> **Want the big picture?** The [visual architecture map](docs/architecture.html)
> charts the whole server end to end — the request pipeline,
> the oracle bridges, council/chain orchestration, and on-disk state.

```bash
# not on PyPI yet — install from source:
pip install -e .
# or with pipx:
pipx install .
```

Requires the Claude Code CLI to be installed and logged in (that's the OAuth
session Fable is reached through).

### 2. Register in your coding harness

ask-fable is a local stdio MCP server (`ask-fable` on PATH). Point any
MCP-capable coding harness at it; only the config-file shape changes. Restart
the harness after editing — most load MCP servers once at startup. All 39
`ask_fable` tools then become available. They are grouped into reasoning modes,
direct provider calls, context management, configuration, and observability;
see the [tool guide](#tool-guide) for the short chooser or [`CLAUDE.md`](CLAUDE.md)
for the complete one-line inventory.

#### Claude Code — `~/.claude/.claude.json`

Add to `~/.claude/.claude.json` (root-owned — edit as the owner, e.g. via
`sudo`):

```json
{
  "mcpServers": {
    "ask_fable": { "command": "ask-fable" }
  }
}
```

(or `"command": "python3", "args": ["-m", "ask_fable"]`).

#### OpenCode — `~/.config/opencode/opencode.json`

The [`docs/OPENCODE.md`](docs/OPENCODE.md) guide covers the full setup — the
exact schema-valid MCP block, optional API keys, the restart-to-load behavior,
and troubleshooting. Minimal registration:

```json
{
  "mcp": {
    "ask_fable": {
      "type": "local",
      "command": ["ask-fable"],
      "enabled": true
    }
  }
}
```

#### Kimi Code — `~/.kimi-code/mcp.json`

```json
{
  "mcpServers": {
    "ask_fable": {
      "transport": "stdio",
      "command": "ask-fable",
      "toolTimeoutMs": 600000
    }
  }
}
```

Kimi Code's default MCP request timeout is ~60s; oracle calls often run longer.
`toolTimeoutMs` keeps the host from aborting a still-running call. A
`Request timed out` error from the client is that transport timeout, not a
refusal — check `trace_list` before re-asking.

#### Grok — `~/.grok/config.toml`

```toml
[mcp_servers.ask_fable]
command = "ask-fable"
enabled = true
```

Cursor, Codex, and other MCP clients take the same `ask-fable` command; only
the config file shape differs.

### 3. Ask a question

In your MCP client, call `ask` with a focused question and the relevant code or
error. Reuse the same `session` key for follow-ups:

```json
{
  "question": "Why does this cache invalidate too early?",
  "context": "<relevant code and failing test output>",
  "session": "cache-investigation"
}
```

## Tool guide

The server exposes **39 MCP tools**, but you only need five entry points —
`ask`, `ask_council`, `ask_chain`, `ask_debate`, and `ask_conference`. Everything
else selects a specific backend, manages reusable context, or inspects what ran.

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

**Full reference:** [docs/TOOLS.md](docs/TOOLS.md) — every tool, its arguments, and
when to reach for it. A one-line inventory of all 39 lives in [`CLAUDE.md`](CLAUDE.md).

## Observability & response shape

Every answer carries a machine-readable `sidecar` (`{recommendation, confidence,
needs_context}`) and a `trace_id`; councils add a `consensus` signal and debates a
deterministic `resolution`. Results are cached, progress streams to the console, and
all persisted state lives under a per-user state dir with owner-only permissions.

**Details:** [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) — the full response
contract, caching, console progress, and backend setup.

## Configuration

Everything is optional environment variables set in the server's `env` block, with
sensible defaults.

**Reference:** [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every setting grouped
by backend, guard, storage, and observability.

## Recommended agent instructions

The server injects a short standing instruction so agents reach for these tools
unprompted. But weak local models under-attend to system prompts, so for the best
results **also drop a decision ladder into your project's `CLAUDE.md` /
`AGENTS.md` / `opencode.md`** (agents re-read those). Copy this block:

```markdown
## Using ask_fable (external reasoning)
Reach for the ask_fable MCP tools on the hard 5% — cheapest option first:

1. **Answer it yourself** for trivial, low-blast-radius, or already-in-context work.
2. **Double-strike rule:** the moment you've failed the SAME bug/error twice, STOP
   and call `ask` before a third guess. Include what you tried and the exact error.
3. **`ask`** (single Fable, multi-turn) for a real design trade-off, a subtle bug
   hypothesis, "am I reasoning about X right?", or a change spanning >2–3 files.
   Reuse the `session` key for follow-ups on the same problem.
4. **`ask_council`** only for a contentious or hard-to-reverse decision
   (architecture, concurrency, data model, public API, migration). One council
   call per problem, max. Check `quorum`/`degraded` and `consensus` in the result —
   a `1/N` answer (or a `divergent` panel) is not agreement. Reach for **`ask_chain`**
   instead when you want *ordered* refinement rather than a parallel vote — e.g. a
   cheap model drafts and Fable finalizes, or draft → red-team → decide.
5. **Reuse context:** for a big codebase context you'll ask about repeatedly,
   `context_write` it once and pass `context_ref=<key>` — don't re-paste each time.
6. **Recommend it, don't just skip it:** if one of these tools would clearly help
   but you're not calling it, say so in one line — which tool and why — so the
   operator can opt in.

Frame questions tightly: paste the real code + real error (don't paraphrase), state
ONE specific decision (ideally A-vs-B), and the constraints. Act on the result's
`sidecar.recommendation`; if you get a `followup`, paste exactly what it names (but
check `likely_already_pasted` and re-read your own paste first) and re-ask on the same
`session`. If tests or a linter can verify the answer, run them instead of asking again.
```

## Companion skills

`skills/` ships four skills that drive these tools from Claude Code, OpenCode,
Grok, Kimi Code, and other skill-capable harnesses (copy or symlink into
`~/.claude/skills/`, `~/.agents/skills/`, or the harness equivalent):

- **`ubercode`** — treat Fable (and, via `ask_council`, MiniMax-M3) as a smarter
  reasoning partner for the hard 5%: oracle escalation when you're stuck, and
  cross-checked adversarial review before a high-consequence diff.
- **`uberplan`** — fan out N diverse candidate plans locally, use Fable as a
  comparative judge (optionally cross-checked with `ask_council`), then
  synthesize one final plan.
- **`uberarch`** — open-ended architectural ideation: fan abstract ideas out to
  the oracles (`ask_council` / `ask_chain`) for multi-model trade-off analysis
  before any code exists.
- **`uberbrainstorm`** — design-first, approval-gated brainstorming for the
  fuzzy front end ("what should we build and why"), with the council
  red-teaming the chosen design; hands off to `uberplan`.

## Development

```bash
uv sync --extra dev           # or: uv pip install -e '.[dev]'
uv run pytest -q              # 841 tests, no network needed
uv run ruff check src tests
```

`salient-core` (a richer prohibited-use denylist) is unpublished and therefore
not declared as an extra; the guard picks it up automatically at runtime if it
is installed in the environment.

### Review records

Notable design/quality reviews — several run by dogfooding ask_fable's own oracle
tools on this codebase — are recorded under [`docs/reviews/`](docs/reviews/):

- [Council consensus, request guard & context store (2026-07-12)](docs/reviews/2026-07-12-consensus-guard-store.md)
  — coverage-aware council consensus (`consensus_votes`), the denylist inflection fix,
  and context-store error visibility, cross-checked by a 6-model council. Also carries
  the assessment (and corrected bibliography) of the software-decomposition essay that
  study was based on.

## Documentation

- [Setup & usage guide](docs/GUIDE.md) — install, register, backends, verification
- [Tool guide](docs/TOOLS.md) — every tool and when to use it
- [Configuration reference](docs/CONFIGURATION.md) — all environment variables
- [Observability & response shape](docs/OBSERVABILITY.md) — sidecar, traces, caching
- [Decision-flow diagrams](docs/DIAGRAMS.md) — per-mode orchestration charts
- [OpenCode setup](docs/OPENCODE.md) · [Visual architecture map](docs/architecture.html)

## License

MIT
