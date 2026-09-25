# ask-fable — idea backlog & tracking

A living list of candidate improvements, with status and code anchors. Ideas were
sparked by comparing ask-fable against an external terminal-oracle MCP project
(`Lykhoyda/ask-llm`) and refined by an M3 review; **every `file:line` below was
verified against the tree on 2026-09-14** (M3's original pointers were mostly right;
the three it got wrong are already corrected here). Nothing is scheduled — this is a
reference to pull from.

**Status legend:** 🟢 build-ready · 🟡 considering · ⚪ later · 💤 idea-only / needs a call · ✅ done

_Last updated: 2026-09-20._ (J and K were added and verified against the tree that day;
everything else is the 2026-09-14 sweep.)


> **A + B + C implemented** on branch `feat/diagnose-quota-hash` (three bisectable
> commits, off `main`). B turned out far smaller than first written: the `rate_limit`
> classifier already existed (`http_error_detail` / `cli_error_detail`), so only the
> breaker's flat cooldown was the gap — now an opt-in per-oracle quota hold. C reused
> the existing `capture_content` and stayed schema v2 (additive `output` block).

## Backlog

| # | Idea | Status | Priority | One-liner |
|---|------|--------|----------|-----------|
| A | `diagnose` / provider-doctor tool | ✅ done | high | one read-only tool: which oracles are up, versions, models, timeouts, `fix:` lines, ok/warning/error rollup (commit da88b01) |
| B | Quota-aware breaker hold | ✅ done | high | `rate_limit` was ALREADY classified; added an opt-in escalating quota hold (`ASK_FABLE_QUOTA_HOLD`) that keeps `state()` pure-health (commit f663703) |
| C | Per-oracle raw-answer hashing | ✅ done | low | per-source answer sha256 on the `provider.completed` event, reusing `capture_content` (commit b454d4e) |
| D | Model-attribution integrity gate | 🟡 | med | when a gateway serves ≠ the requested model, flag it and drop it from the lab/consensus count |
| E | Tolerant sidecar parsing + review schema | 🟡 | med | brace-scanner sidecar extraction (survives surrounding prose) + optional structured findings schema |
| F | Truncation accounting | 🟢 | — | folded into A — signal when tool output was trimmed (`truncations`/`completeness`) |
| G | MCP Resources for stats/traces/health | ⚪ | low | expose live read-only Resources; ask-fable is tools-only today |
| H | Continuous debounced review w/ fail-open stop-gate | 💤 | — | a skill/plugin, not a server change; the "every hook path exits 0" principle is the lesson |
| I | Opt-in "CLI reads real dirs" mode | 💤 | — | cuts against the deliberate "no tools, reason from context" design; needs an explicit decision |
| J | Configurable Fable transport (no-Claude-Code fallback) | ✅ done | med | `ASK_FABLE_FABLE_TRANSPORT=auto/sdk/cli/http` — let `fable` (and the rest of the OAuth family, including `ask_websearch`) answer over an Anthropic API key when the local Claude Code is absent. **Phases 1+2 built 2026-09-20** (selector + http transport; server-side web search over http). Phase 3 = public snapshot sync only — the LobeHub half was dropped 2026-09-25 |
| K | `diagnose` is blind to a missing Claude Code | ✅ done | med | OAuth oracles reported `ok` from the session alone, so a host with no `claude` CLI got no `fix:` line while every call failed `binary_missing`. Now a presence probe (SDK/PATH), with the http transport accepted when pinned+keyed |

## Recommended first slice: A + B + C

A coherent, low-risk observability + robustness upgrade. Build order matters because B is
the only one that changes runtime behaviour outside its own scope.

### A — `diagnose` tool 🟢 (build first: pure read-only, no blast radius)

New module `src/ask_fable/diagnose.py`; register in `server.py` next to `host_status`.
Probe each backend and return a structured health report + remediation: reachable? (CLI
on PATH + `--version`, API key present, gateway configured), resolved model, timeout, and
a `fix:` line when down. Rollup: `error` if any required provider is down or the default
model is `not_configured`; `warning` if only optional providers are down; else `ok`.

Reuse (verified): `oracle_common.timeout_default`/`gemini_timeout_default`/
`codex_timeout_default` (`oracle_common.py:96/104/117`), `kimi.available()` (`kimi.py:152`),
per-provider `configured()` (pattern at `openrouter.py:101`), `controlpage.status()`
(`controlpage.py:93`) for the local GPU leg, plus `oracles.available()` / `oracles.label()`.
Fold in **F**: cap and tag `diagnose`'s own output (`truncations`/`completeness`) since it's
the highest-stakes tool output we hand a model.

Acceptance: returns within ~5s for the standard probe set; each down provider names the
exact env var / PATH fix; rollup thresholds as above. Unit-test with `shutil.which`
monkeypatched.

### C — per-oracle raw-answer hashing 🟢 (build second: low risk)

**Corrected scope (was overstated).** The aggregate tool payload is *already* hashed into
the trace at `trace_runtime.py:201-209` → `orchestration.output.sha256`; `audit.py:147`
already hashes the question. The only gap is a per-source hash of each oracle's raw answer.
**Reuse the existing `capture_content()` helper (`telemetry.py:58`, returns a `ContentCapture`
with `sha256`) on each `OracleResult.text`** and stash the digest on the per-source provider
event — do **not** hand-roll a `response_sha256` field, and note `ProviderTelemetry` lives in
`provider_telemetry.py:89` (not `telemetry.py`). Buys source-level dedup + tamper-evidence.

Acceptance: same oracle, identical stubbed answer → equal digest; one byte changed → differs;
digest appears on the per-source record.

### B — quota-aware breaker hold ✅ (as built, commit f663703)

**Scope was overstated.** A `rate_limit` kind and its classifiers ALREADY existed
(`oracle_common.http_error_detail` for 429/quota/usage-limit; `cli_error_detail`, which
already unions stdout+stderr and parses JSON error envelopes) — so no new kind or classifier
was needed. The only gap: the breaker used one flat cooldown for every health-affecting
failure.

Built (per the triple-flames council, Fable + Opus 5 chose this over storing the tripping
kind): an opt-in **separate quota hold** rather than a kind-aware cooldown, so the breaker's
`state()` stays **pure-health** and `diagnose` reads a true signal. With
`ASK_FABLE_QUOTA_HOLD=<seconds>` set, a `rate_limit` sets `_State.held_until` (escalating ×2
up to 1h, ±10% jitter), which `should_skip` honors, and does **not** feed the health window;
any success lifts it. Monotone (`max()` never shortens); the skip is reported `kind=circuit_open`
but worded as a quota hold with remaining time. Default `0` = off: a `rate_limit` feeds the
window and trips exactly as before, and a stale hold is ignored (a parametrized on==off test
over every other kind proves it). Follow-ups left for later: honor `Retry-After`, a
`quota_group` for the shared-account OAuth models, and an `observe` flag mode.

## Second-round candidates

- **D — attribution integrity 🟡:** flag/exclude a gateway (atlas/openrouter) that serves a
  different model than requested; natural extension of the lab-diversity gate already landed
  (`oracles.lab_of` / `distinct_labs`; downgrade in `server.py` `_council_envelope` /
  `_council`). Needs a reliable per-provider "served model" read, which some CLIs don't give.
- **E — tolerant sidecar + review schema 🟡:** replace regex sidecar extraction with a
  brace/string-state scanner (survives prose around the JSON); optionally offer a structured
  findings schema (severity/confidence/evidence/file/line) for review-shaped councils. The
  current light sidecar is a deliberate choice, so this is speculative.

## Later / idea-only

- **G — MCP Resources ⚪:** expose stats/traces/health as live read-only MCP Resources
  (ask-fable is tools-only today). Low urgency — agents use tools fine.
- **H — continuous review 💤:** a `/loop`- or hook-driven skill running a council or
  `ask_falsify` over each diff, debounced, auto-pausing on quota, engineered to fail open.
  Powerful, but a skills/plugin effort; the durable lesson is "every hook path exits 0 so a
  bug can't wedge turn-end."
- **I — CLI reads real dirs 💤:** let CLI-backed oracles open files instead of relying on
  pasted `context`. Real capability, but a philosophy/security call — the guard never scans
  what a CLI reads on its own — so it needs an explicit decision, not a quiet add.

## Configurable Fable transport (2026-09-20)

### J — survive a host with no Claude Code 🟡

**Design red-teamed by Fable (2026-09-20)** before it was built — the review caught a real
mis-route in the first draft: resolving the transport inside `_dispatch`, which runs twice
per call, would let one question be answered over two different transports.

**The gap.** The entire Anthropic-OAuth family — `fable`, `fable51`, `opus`, `sonnet`,
`opus48`, and the `fable` / Claude seats of `ask_websearch` — is reachable *only* through
Claude Code. `fable.run` prefers the in-process Agent SDK and falls back to the `claude`
CLI (`fable.py:258-265`), both riding `~/.claude/.credentials.json`; with neither present
every one of those oracles dies as `binary_missing` (`fable.py:496`, `fable.py:533`) — even
on a host that holds a working `ANTHROPIC_API_KEY` and a network route. That is deliberate
today (`fable.py:10-11`: "we deliberately do NOT set `ANTHROPIC_API_KEY`"), and
`anthropic_http.py` serves only glm/deepseek (`_DEFAULTS`, `anthropic_http.py:42-47`).

**Design sketch (recommended).** Reuse `anthropic_http` as a third transport instead of
writing a fourth client: it already speaks the Messages shape over stdlib urllib
(`anthropic_http.py:81`), builds its config purely from env (`config_for`,
`anthropic_http.py:66-78`), and honors the shared `REFUSED:` scope contract.

1. New selector on the fable bridge: `ASK_FABLE_FABLE_TRANSPORT = auto/sdk/cli/http`,
   default `auto` (today's SDK → CLI ladder). An unrecognized value is a hard error, never a
   silent fallback.
2. `http` = the `anthropic` provider: `ASK_FABLE_ANTHROPIC_API_KEY` / `_BASE_URL` (default
   `https://api.anthropic.com`) / `_MODEL`, resolved through `config_for("anthropic")`.
3. `auto` treats "Claude Code absent" as a *demotion*, exactly like the model ladder already
   does (`_unavailable`, `fable.py:78`): fall through to http, and record which transport
   answered in `ProviderTelemetry.transport` — that field already exists, so a mixed fleet
   stays legible instead of silently answering from somewhere else.
4. **Scope (open):** transport-level rather than fable-only. `opus.py` / `fable51.py` /
   `anthropic_variants.BRIDGES` are all the same bridge (`oracles.py:107`), so one knob
   covers the family — but it also means the HTTP path must keep the per-spec model ids and
   the ladder working.

**Why it matters for `ask_websearch`.** The Claude seats pass `WebSearch`/`WebFetch` as
*client* tools through the Agent SDK (`websearch.py:140` → `fable._run_sdk`); over plain
Messages HTTP those do not exist. Anthropic's API does publish a **server-side web search
tool** for exactly this — **verify the current tool id/version against live API docs before
designing on it** (the `anthropic` python SDK is not installed here, so there was nothing
local to check it against). If it holds, `model="fable"` in `ask_websearch` works on a host
with no CLI at all, which is the case J exists for.

**Acceptance.** On a host with no `claude` CLI and no importable `claude_agent_sdk`, with
`ASK_FABLE_ANTHROPIC_API_KEY` set and the transport pinned to `http`: `ask` and
`ask_websearch(model="fable")` both return `status:"ok"`, and the telemetry names the http
transport. With the key absent, the error names every transport tried (`sdk`/`cli`/`http`)
rather than a bare `binary_missing`. Tests: `shutil.which` → None plus a blocked SDK import,
asserting the demotion order and the final error text.

**Open questions.** (a) Does a pinned `ASK_FABLE_FABLE_MODEL` imply a transport? (b) Should
`diagnose` gain the API key as a second reachability signal (see K)? (c) Prompt parity —
`FABLE_SYSTEM_PROMPT` vs `ORACLE_SYSTEM_PROMPT` on the http path, and whether `resume=`
(multi-turn, SDK-only today) is expected to work there (it cannot — Messages HTTP would need
its own transcript replay, which `fable.py:14-15` explicitly avoids).

### K — `diagnose` says OAuth backends are fine when Claude Code isn't installed 🟢

`diagnose.py:155-158` short-circuits every `_OAUTH_KEYS` oracle to
`{"name": "oauth", "ok": True, "detail": "Claude Code OAuth session"}` — deliberately, so the
probe never touches the token. The side effect: on a host with no `claude` CLI and no SDK the
doctor prints **ok** with no `fix:` line, while every real call fails `binary_missing`
(`fable.py:496/533`). That is the exact host J is about, so a J roll-out would look healthy
and still fail.

Cheapest correct probe: the same two signals `fable.run` already uses — `shutil.which("claude")`
and an import check of `claude_agent_sdk` — reported as `detail` with a `fix:` line pointing at
J's http transport. Still no network, still no token refresh. Small enough to land with J.

## Already covered (don't reinvent)

- Prompt-injection hardening — context/debate input framed as untrusted in `prompts.py`.
- Mechanical consensus that confidence can't inflate — `_consensus` + the lab-diversity gate.
- Usage/cost by provider+model — `stats`, `cost_usd_notional`, audit log.
- Rich failure taxonomy — `auth_failed`/`network_error`/`timeout`/`not_configured`/
  `circuit_open`/`busy`/`context_too_large`/… (the only missing kind is quota → idea B).
- Kimi argv cap — already handled; that CLI has no stdin prompt mode, so stdin delivery
  isn't an option there specifically.
