# Observability & response shape

> Part of the [ask-fable README](../README.md).


Every MCP call receives a correlated `trace_id`. **Safe mode** (the default) writes
schema-v2 metadata only: no raw prompt or answer is included in that trace. The separate
legacy audit log can store raw values only when its explicit `ASK_FABLE_AUDIT_RAW` switch
is enabled. **Full mode** writes a redacted, size-capped trace bundle under
`${XDG_STATE_HOME}/ask_fable/traces/`; it may include provider-emitted reasoning and tool
activity when available. `trace_list` finds recent calls and `trace_get` reads a timeline
or a bounded bundle excerpt.

Answer Markdown is separate: `ASK_FABLE_SAVE=1` saves every successful answer under
`${XDG_STATE_HOME}/ask_fable/answers/` (override with `ASK_FABLE_OUTPUT_DIR`),
`ASK_FABLE_SAVE=0` disables it, and an unset setting saves only in full trace mode.
Its path is returned as `"saved"`. Saved files include a `## Thinking` section only
when a provider emitted reasoning. This is independent of `reset_session` dumps.

### Response contract

Every tool returns one JSON object. A successful answer looks like this:

```json
{
  "status": "ok",
  "answer": "…",
  "sidecar": {
    "recommendation": "apply",
    "confidence": "high",
    "needs_context": []
  },
  "trace_id": "…",
  "telemetry": { "status": "ok" }
}
```

`sidecar` is `{recommendation, confidence, needs_context}` (null when the model emitted
no parseable one). When the model wants more it also carries
`"followup":{"needs_context":[...],"how":...,"likely_already_pasted":[...]}`, and a
stuck re-ask loop terminates with `"status":"context_exhausted"` (+ best-effort answer;
tune the cap with `ASK_FABLE_MAX_NEEDS_CONTEXT`, default 2). Any `context_ref` keys used
are echoed as `"context_ref_resolved":[...]` / `"context_ref_missing":[...]`; an
all-missing ref with no other context returns `"status":"needs_context"` (+ a
`did_you_mean` suggestion) without calling the model.

**Councils** add `"mode":"council"`, `"synthesizer"`, `"sources"` (each entry with that
model's `recommendation`), the `"consensus"`/`"material_disagreement"` signal, plus a
small **envelope** so you can tell whether the council degraded: `"quorum":"N/M"`
(answered / asked), `"effective_models":[...]`, `"degraded":bool`,
`"confidence":"high|medium|low"`, and `"recommended_next_action":...` — a `1/M` quorum is
one opinion, not consensus.

**Chains** (`ask_chain`) add `"mode":"chain"`, the `"pipeline"` (ordered model labels),
`"answered_by"`, a lean `"stages"` list (each with `stage`/`model`/`role`/`status`/
`recommendation`/`confidence`), the `"recommendation_drift"` trail + `"material_drift"`
flag, and `"answered":N`/`"requested":M`; a `"fallback"` note appears when a failed final
stage was reconciled by Fable.

**Debates** (`ask_debate`) add `"mode":"debate"`, `"answered_by"`, a lean `"turns"` list
(each with `role`/`model`/`status`/`recommendation`/`confidence`), and a `"debate"` block:
`"pairing"`, `"rounds"`, `"resolution"` (`conceded`/`converged`/`adjudicated`/`stalemate`/
`degraded_single_critic`), `"contested_claims_remaining"`, `"recommendation_drift"`,
`"low_effort_opposition"`, `"material_disagreement"`, and `"decisive_argument"` (the
adjudicator's quoted pivot). The full transcript goes to the saved markdown file, not the
inline reply. Shares the `ASK_FABLE_CHAIN_TIMEOUT` wall-clock bound.

**Failure responses** are structured too:

```json
{ "status": "refused", "stage": "guard", "reason": "…" }
```

```json
{ "status": "error", "kind": "timeout", "detail": "…" }
```

### Caching

The single-shot tools (`ask_m3`/`ask_glm`/`ask_deepseek`/`ask_gemini`/`ask_codex`/`ask_grok`/`ask_kimi`/`ask_ollama`/`ask_atlas`/`ask_openrouter`), the councils, and
`ask_chain` (keyed on the **ordered** pipeline) **cache**
successful answers keyed on `hash(tool + models + normalized question + context)`.
An exact re-ask within the freshness window returns instantly with `"cached":true`,
`"cache_age_s":N`, and a duplicate-nudge `"note"` — so a local agent's edit/verify
re-ask loop doesn't pay for the model every time. `ask` (multi-turn) is never cached.
Tune with `ASK_FABLE_CACHE_TTL` (seconds, default 3600) or disable with
`ASK_FABLE_CACHE=0`.

### Console progress

All ask tools print a tidy, TTY-colored trace of what's happening — guard
result, each model being asked, elapsed time, reasoning excerpts, and the
synthesis step — to **stderr** (Claude Code surfaces this in its MCP logs /
`claude --debug`; in
a terminal it prints live). stdout is reserved for the JSON-RPC protocol. Silence
it with `ASK_FABLE_QUIET=1`; hide just the model reasoning with
`ASK_FABLE_SHOW_REASONING=0`. Stream Fable's reasoning **live** (block by block, as it
arrives) with `ASK_FABLE_STREAM_REASONING=1` instead of one post-hoc excerpt — Fable
only, since the other backends don't stream. To surface a reasoning excerpt **inline in
the tool result** (so it shows in the Claude Code conversation, not just the stderr
trace), set `ASK_FABLE_RETURN_THINKING=1`, capped by `ASK_FABLE_THINKING_CHARS` (default
4000). Full trace bundles are written only in full trace mode; answer Markdown follows
the `ASK_FABLE_SAVE` policy described above.

### Backend setup

For `ask_council`'s **MiniMax** oracle, install the MiniMax `mmx` CLI and log in
once (`mmx auth login`) — the server sets no key, it reuses that session exactly
as the Fable bridge reuses Claude Code's OAuth. `ask-fable` always passes
`--model MiniMax-M3` explicitly, but the `mmx` CLI's own default is older
(`MiniMax-M2.7`); standardize it once with
`mmx config set --key default_text_model --value MiniMax-M3` so ad-hoc `mmx`
calls match. The **Gemini** oracle works the same way: install the `agy` CLI and
sign in once — the server sets no key and reuses that session. `ask-fable` calls it
in non-interactive print mode (`agy --model "Gemini 3.1 Pro (High)" -p "<prompt>"`)
and reads the plain-text answer from stdout. Pick a different `agy` model (run
`agy models` to list them) with `ASK_FABLE_GEMINI_MODEL`. The **Codex** oracle
works the same way: install OpenAI's `codex` CLI and run `codex login` once — the
server sets no key and reuses that session. `ask-fable` calls it non-interactively
(`codex exec`) with a **hermetic, read-only** invocation (`--ignore-user-config`
`--sandbox read-only`), so the operator's own `~/.codex/config.toml` and hooks
can't change the answer and it can't touch the repo. Pick a different model with
`ASK_FABLE_CODEX_MODEL` and its reasoning effort with `ASK_FABLE_CODEX_REASONING`
(default `high`). The **Ollama Cloud**
oracles work
the same way: install `ollama`, run `ollama signin` once, and the local daemon
proxies `:cloud` models — **no API key needed** (this is the default;
`ASK_FABLE_OLLAMA_BASE_URL=http://localhost:11434`). To hit `ollama.com` directly
instead, set `ASK_FABLE_OLLAMA_BASE_URL=https://ollama.com` and an
`ASK_FABLE_OLLAMA_API_KEY`. The **GLM** and **DeepSeek** oracles are
Anthropic-Messages-compatible HTTP endpoints, while Atlas uses the OpenAI chat
shape; enable them by putting their keys in the server's registration `env` (in
`~/.claude.json`, kept out of the repo), e.g.:

```json
{ "mcpServers": { "ask_fable": { "command": "ask-fable", "env": {
  "ASK_FABLE_GLM_API_KEY": "<z.ai key>",
  "ASK_FABLE_DEEPSEEK_API_KEY": "<deepseek key>",
  "ASK_FABLE_ATLAS_API_KEY": "<Atlas Cloud key>"
} } } }
```
