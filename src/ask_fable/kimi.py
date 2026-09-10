"""Invoke a Moonshot Kimi model (kimi-code/k3 by default) for one reasoning turn.

Another single-model oracle, reached the same way as MiniMax / Gemini / Codex /
Grok: shell out to an already-authenticated CLI — here the local ``kimi`` binary
(Kimi Code) in single-turn ``-p`` mode. No API key is set; we re-use the host
login (``kimi login`` / the ``managed:kimi-code`` OAuth provider).

Prefer this bridge over ``ask_atlas`` with ``moonshotai/kimi-*`` whenever the
binary is on PATH: same model family, no Atlas API key, operator's existing
subscription instead of per-token billing.

Caveat on context: ``kimi-code/k3`` is a 1M-context MODEL, but this TRANSPORT is
not — the CLI accepts the prompt only as an argv value, which the kernel caps at
~131k bytes (see ``MAX_PROMPT_BYTES``). Prompts above that are refused here with
a pointer to the HTTP route, which has no such limit.

THE SANDBOX HOME IS THE WHOLE TRICK. Unlike ``grok``, the ``kimi`` CLI has no
``--disallowed-tools`` / ``--system-prompt-override`` / ``--permission-mode``
flags, and its ``-p`` mode is a fully agentic loop: asked a repo-flavored
question it will leave the working directory, walk the real filesystem with
ls/grep/read, take minutes instead of seconds, and splice raw tool output into
the ``stream-json`` stream as non-JSON lines. That breaks the oracle contract —
these models must reason only over what the caller put in ``context``.

Both missing knobs are recoverable through ``KIMI_CODE_HOME``, which relocates
the CLI's whole config root (``resolveKimiHome``). We therefore run every turn
against a generated home of our own:

- NO ``workspace-trust``. THIS IS THE GUARD THAT ACTUALLY HOLDS, and it holds by
  omission: the file is simply never linked, so no directory is trusted and the
  agent loop cannot execute tools. Measured on kimi-code 0.39.1, asking a
  repo-flavored question from inside a real repo:

      trust + no rules  -> 6 tool calls, 59 non-JSON lines leaked
      trust + rules     -> 6 tool calls, 12 non-JSON lines leaked
      no trust + rules  -> 0 tool calls,  0 leakage

  So do not "fix" a future permission problem by linking ``workspace-trust`` in,
  and do not assume the rules below are load-bearing on their own — they are not.
- ``config.toml`` — the operator's providers/models/services copied verbatim (that
  is what the OAuth login needs), plus a ``[[permission.rules]]`` entry of
  ``decision = "deny"`` / ``pattern = "*"`` (``*`` is the parser's match-any tool
  name). Defense in depth only, per the measurements above.
- ``SYSTEM.md`` — the shared scope contract. A non-empty ``$KIMI_CODE_HOME/SYSTEM.md``
  permanently replaces the builtin profile's system prompt, which is this CLI's
  equivalent of grok's ``--system-prompt-override``.
- ``oauth`` / ``credentials`` / ``device_id`` / ``region`` — symlinked back to the real
  home so we reuse the existing login rather than forcing a second ``kimi login``.

The home is built per reasoning effort (``[thinking] effort`` is config-only, with
no CLI flag) so a call never rewrites a config another concurrent call is reading.

Output is parsed from ``--output-format stream-json``: one JSON object per line,
where ``role="assistant"`` carries the answer and ``role="meta"`` is version and
resume-hint noise. Parsing stays defensive about non-JSON lines anyway — that is
exactly the leakage the sandbox exists to prevent, so a stray line means the
sandbox failed rather than that the answer is fine.

Like ``agy`` / ``codex`` / ``grok``, the CLI can spawn children and would block
forever on the MCP server's open stdin pipe, so we use ``stdin=DEVNULL`` and a
process-group SIGKILL on timeout.

Pure reasoning: no session to resume.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from . import _paths, cli_gate
from .oracle_common import OracleResult, cli_error_detail, compose, shape, timeout_default
from .prompts import ORACLE_SYSTEM_PROMPT
from .provider_telemetry import ProviderTelemetry

# k3, not a k2.x coding alias: it is the 1M-context model, which is the reason to
# prefer the local CLI over Atlas's 262k kimi-k2.x in the first place.
# The CLI takes the prompt ONLY as an argv value — it has no stdin prompt mode
# (`-p -` is read as the literal string "-"). Linux caps a single argv element at
# MAX_ARG_STRLEN = 32 pages = 131072 bytes, measured here as 131071 usable, and
# `Popen` raises OSError(E2BIG) above it. So while the MODEL takes 1M tokens, this
# TRANSPORT does not: oversized prompts are rejected up front with a pointer to
# the HTTP route rather than escaping as an uncaught OSError.
MAX_PROMPT_BYTES = 120_000  # margin under 131071 for the rest of argv
DEFAULT_KIMI_MODEL = "kimi-code/k3"
DEFAULT_KIMI_EFFORT = "high"
KIMI_BINARY = "kimi"

# The CLI's own vocabulary (config `support_efforts`), not Atlas's quick/standard/deep.
_KIMI_EFFORTS = ("low", "high", "max")
_EFFORT_MAP = {
    "quick": "low",
    "standard": "high",
    "deep": "max",
    "low": "low",
    "medium": "high",
    "high": "high",
    "max": "max",
}

# Atlas ids → local model aliases. Atlas exposes moonshotai/kimi-k2.5|k2.6|
# k2.7-code|k3; the local subscription exposes the kimi-code/* aliases. Anything
# unmapped falls back to the default rather than passing an alias the CLI would
# reject.
_ATLAS_TO_LOCAL = {
    "kimi-k3": "kimi-code/k3",
    "kimi-k2.7-code": "kimi-code/kimi-for-coding",
    "kimi-k2.6": "kimi-code/kimi-for-coding",
    "kimi-k2.5": "kimi-code/kimi-for-coding",
}


def kimi_model() -> str:
    return (os.environ.get("ASK_FABLE_KIMI_MODEL") or "").strip() or DEFAULT_KIMI_MODEL


def kimi_effort() -> str:
    """Default thinking effort when no per-call effort is passed.

    Precedence: ``ASK_FABLE_KIMI_EFFORT`` → ``ASK_FABLE_EFFORT`` (mapped) → ``high``.
    """
    explicit = (os.environ.get("ASK_FABLE_KIMI_EFFORT") or "").strip().lower()
    if explicit:
        # Map first: ASK_FABLE_KIMI_EFFORT must accept the same quick/standard/deep
        # vocabulary as ASK_FABLE_EFFORT, or setting the specific var would land on
        # the OPPOSITE end of the scale from the global one ('quick' -> high, not low).
        return _EFFORT_MAP.get(explicit, explicit if explicit in _KIMI_EFFORTS
                               else DEFAULT_KIMI_EFFORT)
    global_effort = (os.environ.get("ASK_FABLE_EFFORT") or "").strip().lower()
    if global_effort:
        return _EFFORT_MAP.get(global_effort, DEFAULT_KIMI_EFFORT)
    return DEFAULT_KIMI_EFFORT


def kimi_timeout_default() -> float:
    """Prefer ``ASK_FABLE_KIMI_TIMEOUT`` so the agentic CLI can be capped without
    lowering the global timeout."""
    for var in ("ASK_FABLE_KIMI_TIMEOUT", "ASK_FABLE_TIMEOUT"):
        raw = os.environ.get(var)
        if raw:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    return timeout_default()


def available() -> bool:
    """True when the CLI is installed AND its login material exists — an
    unauthenticated CLI would sit at a device-code prompt we cannot answer."""
    if shutil.which(KIMI_BINARY) is None:
        return False
    return (real_home() / "oauth").exists() or (real_home() / "credentials").exists()


def looks_like_kimi_model(model: str) -> bool:
    """True when an Atlas (or bare) model id is a Moonshot Kimi model we can serve
    locally."""
    m = (model or "").strip().lower()
    if not m:
        return False
    for prefix in ("moonshotai/", "moonshot/", "kimi-code/"):
        if m.startswith(prefix):
            return True
    return m.startswith("kimi")


def local_alias_for(model: str) -> str | None:
    """The local CLI alias for ``model``, or None when we cannot map it.

    None means "do not serve this locally". Silently substituting the default for
    an unrecognized id would answer a council member the caller never asked for
    while still reporting the requested id — a wrong attribution, not a fallback.
    """
    m = (model or "").strip()
    if not m:
        return None
    if m.lower().startswith("kimi-code/"):
        return m  # already a local alias
    bare = m.split("/", 1)[1] if "/" in m else m
    return _ATLAS_TO_LOCAL.get(bare.lower())


def local_model_for(model: str) -> str:
    """Like :func:`local_alias_for` but falling back to the configured default —
    for ``ask_kimi``, where no specific model was requested."""
    return local_alias_for(model) or kimi_model()


def real_home() -> Path:
    """The operator's own Kimi Code home — the source of the login material."""
    raw = (os.environ.get("ASK_FABLE_KIMI_HOME") or os.environ.get("KIMI_CODE_HOME") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".kimi-code"


# `[table]` / `[[array.of.tables]]` on its own line — NOT a bracketed array row.
_TABLE_HEADER_RE = re.compile(r"^\[\[?[^\[\]]+\]\]?\s*(?:#.*)?$")


def _sandbox_home(effort: str) -> Path:
    return _paths.xdg_state_dir() / "ask_fable" / "kimi-home" / effort


def _strip_table(text: str, name: str) -> str:
    """Drop a top-level ``[name]`` table from TOML source.

    We re-declare ``[thinking]`` with our own effort, and TOML forbids declaring a
    table twice — so the original has to go first. (``kimi doctor`` accepts the
    duplicate, but the CLI's real loader does not: it silently loses every later
    table, which surfaces as "Model … is not configured in config.toml".)
    """
    out, skipping = [], False
    for line in text.splitlines():
        stripped = line.strip()
        # Only a real header ends (or starts) a table. Testing `startswith("[")`
        # also matches a continuation row of a multi-line array — e.g. a
        # `budgets = [\n  [1, 2],\n]` inside [thinking] — which would end the skip
        # early and leave orphaned array rows at top level (invalid TOML).
        if _TABLE_HEADER_RE.match(stripped):
            skipping = stripped == f"[{name}]"
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _strip_bare_key(text: str, key: str) -> str:
    """Drop a top-level bare ``key = ...`` assignment from TOML source.

    We prepend our own ``default_permission_mode``; if the operator's config
    already sets it (the CLI writes this key itself), emitting it twice is a
    "Cannot overwrite a value" parse error and every kimi call dies on an opaque
    CLI config error. Only the top-level occurrence is removed — an identically
    named key inside a table is a different key and must survive.
    """
    out, in_table = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if _TABLE_HEADER_RE.match(stripped):
            in_table = True
        if not in_table and re.match(rf"{re.escape(key)}\s*=", stripped):
            continue
        out.append(line)
    return "\n".join(out)


def render_config(base: str, effort: str) -> str:
    """Overlay the sandbox settings onto the operator's config source.

    Bare keys are PREPENDED, not appended: in TOML a top-level key written after a
    ``[table]`` header belongs to that table, so appending
    ``default_permission_mode`` would quietly make it a field of whichever model
    alias happened to be last. Table sections are appended, which is position-safe.
    """
    body = _strip_bare_key(_strip_table(base, "thinking"), "default_permission_mode")
    return (
        "# Generated by ask_fable — do not edit; rewritten on every call.\n"
        "# A sandboxed KIMI_CODE_HOME that runs `kimi -p` as a pure text reasoning\n"
        "# oracle. Bare keys must precede the first [table], hence the prepend.\n"
        'default_permission_mode = "manual"\n\n'
        f"{body}\n\n"
        "# `*` is the permission parser's match-any tool name, so the model gets no\n"
        "# tools and must answer from the prompt instead of reading the filesystem.\n"
        "[[permission.rules]]\n"
        'decision = "deny"\n'
        'scope = "user"\n'
        'pattern = "*"\n\n'
        "[thinking]\n"
        "enabled = true\n"
        f'effort = "{effort}"\n'
    )


def _read_or_none(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def build_sandbox(effort: str) -> Path | None:
    """Create (or refresh) the hermetic ``KIMI_CODE_HOME`` for one effort level.

    Returns None when the operator's config cannot be read — the caller then
    reports the oracle as unconfigured rather than falling back to the real home,
    which would let an agentic turn loose on the filesystem.
    """
    src_home = real_home()
    try:
        base = (src_home / "config.toml").read_text()
    except OSError:
        return None

    home = _sandbox_home(effort)
    # 0700/0600, matching every other state writer in the package: this config is
    # a verbatim copy of the operator's, `[providers.*]` blocks and any inline
    # api_key included, and it sits beside symlinks to their OAuth material.
    if not _paths.ensure_dir_secure(home):
        return None
    try:
        # Re-use the real login instead of forcing a second `kimi login`.
        # `workspace-trust` is deliberately absent from this list and MUST STAY
        # absent — it is the guard that actually stops tool execution (see the
        # module docstring for the measured comparison). Linking it in would make
        # this oracle read the caller's filesystem.
        for name in ("oauth", "credentials", "device_id", "region"):
            target, link = src_home / name, home / name
            if not target.exists():
                continue
            if link.is_symlink():
                # Re-point a link that has gone stale (ASK_FABLE_KIMI_HOME changed,
                # or the source moved) or dangling. Skipping every existing symlink
                # made both states permanently unrepairable.
                if link.readlink() == target:
                    continue
                link.unlink()
            elif link.exists():
                continue
            try:
                link.symlink_to(target)
            except FileExistsError:
                pass  # a concurrent call won the race; its link is equivalent

        config = render_config(base, effort)
        # Skip the rewrite when nothing changed: this runs on the event loop for
        # EVERY turn, and the output is byte-identical for a given effort.
        # write_secure is atomic (temp + os.replace), so a concurrent reader sees
        # the old or new file, never a truncated one.
        target_config = home / "config.toml"
        if _read_or_none(target_config) != config:
            if not _paths.write_secure(target_config, config):
                return None
        system_md = ORACLE_SYSTEM_PROMPT + "\n"
        # A non-empty SYSTEM.md replaces the builtin profile prompt — this CLI's
        # only route to grok's --system-prompt-override.
        target_system = home / "SYSTEM.md"
        if _read_or_none(target_system) != system_md:
            if not _paths.write_secure(target_system, system_md):
                return None
    except OSError:
        return None
    return home


def parse_stream_json(stdout: str) -> tuple[str, bool]:
    """Extract the assistant answer from ``--output-format stream-json`` output.

    Returns ``(answer, leaked)``. ``leaked`` is True only when a ``role="tool"``
    event appears — the CLI's own authoritative record that a tool executed, which
    means the turn saw more than the caller passed in.

    Non-JSON lines are NOT treated as leakage on their own. Raw tool output does
    land on stdout that way, but so does ordinary CLI chatter (update banners,
    deprecation notices) from a third-party binary we have pinned to no version;
    discarding a good answer and telling the operator their sandbox was breached
    over a version banner is the worse failure. The tool role covers the real case.
    """
    parts: list[str] = []
    leaked = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        role = obj.get("role")
        if role == "assistant":
            content = obj.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(content.strip())
        elif role == "tool":
            leaked = True  # a tool executed despite the deny-all ruleset
    return "\n\n".join(parts).strip(), leaked


def _error_telemetry(
    model: str, *, wall_duration_ms: float = 0.0, returncode: int | None = None
) -> ProviderTelemetry:
    return ProviderTelemetry(
        oracle_key="kimi", requested_model=model, actual_model=model,
        transport="cli-json", returncode=returncode, wall_duration_ms=wall_duration_ms,
        reasoning_available=None, usage_available=None, tools_available=False,
    )


async def run(
    question: str,
    context: str = "",
    *,
    timeout: float | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> OracleResult:
    """Run one Kimi turn via the local ``kimi`` CLI. Never raises for expected failures."""
    timeout = timeout if timeout is not None else kimi_timeout_default()
    requested = model or kimi_model()
    model = local_model_for(requested)
    raw_effort = (effort or "").strip().lower()
    thinking = _EFFORT_MAP.get(raw_effort, raw_effort if raw_effort in _KIMI_EFFORTS else "") \
        or kimi_effort()

    kimi_bin = shutil.which(KIMI_BINARY)
    if not kimi_bin:
        return OracleResult(
            "error",
            kind="binary_missing",
            text=(
                f"`{KIMI_BINARY}` CLI not found on PATH "
                "(install Kimi Code and run `kimi login` to use ask_kimi)"
            ),
            model=model,
            telemetry=_error_telemetry(model),
        )

    home = build_sandbox(thinking)
    if home is None:
        return OracleResult(
            "error", kind="not_configured",
            text=(
                f"could not read {real_home() / 'config.toml'} to build the sandboxed "
                "KIMI_CODE_HOME (run `kimi login` first, or set ASK_FABLE_KIMI_HOME)"
            ),
            model=model, telemetry=_error_telemetry(model),
        )

    # Scope contract rides in SYSTEM.md; user turn is question + code context.
    message = compose(question, context)
    size = len(message.encode("utf-8", "replace"))
    if size > MAX_PROMPT_BYTES:
        return OracleResult(
            "error", kind="context_too_large",
            text=(
                f"prompt is {size} bytes; the local `kimi` CLI takes it as a single "
                f"argv value, which the kernel caps at ~131k (MAX_ARG_STRLEN), so "
                f"ask_fable rejects above {MAX_PROMPT_BYTES}. The model's 1M context "
                "is reachable over HTTP instead: ask_atlas with "
                "'moonshotai/kimi-k3', or trim `context`."
            ),
            model=model, telemetry=_error_telemetry(model),
        )
    argv = [kimi_bin, "-p", message, "--output-format", "stream-json", "-m", model]

    try:
        started = time.perf_counter()
        cap = await cli_gate.run_cli_async(
            argv, gate="kimi", timeout=timeout, env={"KIMI_CODE_HOME": str(home)}
        )
    except FileNotFoundError:
        return OracleResult(
            "error", kind="binary_missing",
            text=f"`{KIMI_BINARY}` CLI not found on PATH",
            model=model, telemetry=_error_telemetry(model),
        )

    wall_ms = (time.perf_counter() - started) * 1000
    if cap.timed_out:
        detail = (
            f"Kimi queued behind ASK_FABLE_CLI_MAX_PARALLEL for {timeout:.0f}s "
            "without getting a slot"
            if cap.queue_timed_out
            else f"Kimi timed out after {timeout:.0f}s"
        )
        return OracleResult(
            "error", kind="timeout", text=detail, model=model,
            telemetry=_error_telemetry(model, wall_duration_ms=wall_ms),
        )
    if cap.returncode != 0:
        kind, detail = cli_error_detail(
            label="Kimi", returncode=cap.returncode, stderr=cap.stderr, stdout=cap.stdout,
        )
        return OracleResult(
            "error", kind=kind, text=detail, model=model, returncode=cap.returncode,
            telemetry=_error_telemetry(model, wall_duration_ms=wall_ms, returncode=cap.returncode),
        )

    answer, leaked = parse_stream_json(cap.stdout)
    if not answer:
        return OracleResult(
            "error", kind="sdk_error", text="Kimi returned no answer", model=model,
            telemetry=_error_telemetry(model, wall_duration_ms=wall_ms),
        )
    if leaked:
        # Loud on purpose: a tool ran, so this turn saw more than the caller passed.
        return OracleResult(
            "error", kind="sdk_error",
            text=(
                "Kimi executed a tool despite the sandboxed deny-all ruleset — the "
                "answer may reflect local filesystem reads rather than the supplied "
                "context, so it is being discarded. Check that "
                f"{home / 'config.toml'} still carries its [[permission.rules]] block."
            ),
            model=model,
            telemetry=_error_telemetry(model, wall_duration_ms=wall_ms),
        )
    shaped = shape(answer)
    shaped.model = model
    shaped.telemetry = ProviderTelemetry(
        oracle_key="kimi", requested_model=requested, actual_model=model,
        transport="cli-json", returncode=cap.returncode, wall_duration_ms=wall_ms,
        reasoning_available=None, usage_available=None, tools_available=False,
    )
    return shaped
