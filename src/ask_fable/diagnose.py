"""Read-only backend health probe behind the ``diagnose`` tool.

Answers the question an operator actually asks — "why is this oracle dark, and how
do I fix it?" — WITHOUT making a paid model call and WITHOUT perturbing any state.

Hard contract (enforced by tests):
- **Never** goes through the real call path (``oracles.run`` / the fan-out), so it
  never runs the classifier, ``record_provider``, or ``breaker.record``. It only
  reads: ``oracles.available`` / ``oracles.label`` (which already encode every
  backend's reachability rule, cheaply and side-effect-free), the read-only
  ``breaker.snapshot``, and a bounded ``<cli> --version`` for CLI bridges.
- **Never** sends a completion or hits a billing endpoint. Reachability is
  configuration presence + (for CLI bridges) that the binary runs. A ``deep`` probe
  that makes a free metadata call is left as a future opt-in, not v1.
- **Bounded**: every ``--version`` runs under a hard timeout in its own process
  group (killed on timeout so a hung CLI cannot wedge the tool), and all probes run
  concurrently, so the tool returns in about one probe's timeout.

Rollup: ``error`` = configured AND broken (a CLI on PATH whose ``--version`` fails);
``warning`` = degraded (breaker open / half-open, or a live quota hold); a backend
that is simply not wired up is ``not_configured`` (never ``error`` — unconfigured by
design is not a fault); else ``ok``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess

from . import fable, oracles
from .codex import CODEX_BINARY
from .gemini import GEMINI_BINARY
from .grok import GROK_BINARY
from .health import breaker
from .kimi import KIMI_BINARY
from .oracle_common import (
    codex_timeout_default,
    gemini_timeout_default,
    timeout_default,
)
from .redaction import redact_text

SCHEMA_VERSION = 1

# CLI-backed oracle keys → the binary whose presence + `--version` we probe.
_CLI_BINARIES: dict[str, str] = {
    "minimax": "mmx",
    "gemini": GEMINI_BINARY,
    "codex": CODEX_BINARY,
    "grok": GROK_BINARY,
    "kimi": KIMI_BINARY,
}

# Oracle keys that ride Claude Code's OAuth session (no key/CLI to configure).
_OAUTH_KEYS = frozenset(getattr(oracles, "_ANTHROPIC", ("fable", "fable51", "opus")))

# A one-line remediation per key, shown when the backend is not reachable.
_FIX: dict[str, str] = {
    "minimax": "install the `mmx` CLI and authenticate it",
    "gemini": "install the `agy` (Antigravity) CLI and log in",
    "codex": "install the `codex` CLI and log in",
    "grok": "install the `grok` CLI and log in",
    "kimi": "install the `kimi` (Kimi Code) CLI and log in",
    "deepseek": "set ASK_FABLE_DEEPSEEK_API_KEY",
    "glm": "set ASK_FABLE_GLM_API_KEY (or an Atlas key for the hosted fallback)",
}
# The Claude-Code-less host fix: there is nothing to install if the operator would
# rather point the family at the API.
_FIX_CLAUDE_CODE = (
    "install Claude Code (`claude-agent-sdk` or the `claude` CLI) and sign in, or set "
    "ASK_FABLE_FABLE_TRANSPORT=http with ASK_FABLE_ANTHROPIC_API_KEY"
)

_PROBE_TIMEOUT_S = 5.0
_TOTAL_DEADLINE_S = 20.0


def _timeout_for(key: str) -> float:
    """The per-turn timeout the LIVE call path would use for this oracle — read
    from the same resolver, never re-derived, so diagnose can't lie about it."""
    if key == "gemini":
        return gemini_timeout_default()
    if key == "codex":
        return codex_timeout_default()
    return timeout_default()


def _cli_version(binary: str, *, timeout: float = _PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """Run ``<binary> --version`` safely and synchronously (call via a thread).

    stdin is closed and update/color/pager env is suppressed so a first-run prompt
    can't block; the child gets its own process group and is killed group-wide on
    timeout so no grandchild is stranded. Returns (ok, detail); detail is the first
    output line on success, or the failure reason. stdout is redacted (CLIs
    occasionally print a token or a config path)."""
    path = shutil.which(binary)
    if not path:
        return False, "not on PATH"
    env = {
        **os.environ,
        "CI": "1",
        "NO_COLOR": "1",
        "NO_UPDATE_NOTIFIER": "1",
        "PAGER": "cat",
    }
    try:
        proc = subprocess.Popen(  # noqa: S603 — a binary we resolved via which()
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # A stray non-UTF-8 byte (a Latin-1 banner, a truncated emoji) must not raise
            # UnicodeDecodeError out of communicate() and sink the whole diagnose report.
            errors="replace",
            start_new_session=True,  # own process group for the kill below
            env=env,
        )
    except OSError as exc:
        return False, f"could not launch `{binary} --version`: {exc}"
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        proc.wait()
        return False, f"`{binary} --version` timed out after {timeout:.0f}s"
    if proc.returncode != 0:
        detail = (err or out or "").strip().splitlines()
        why = redact_text(detail[0])[0] if detail else ""
        return False, f"`{binary} --version` exited {proc.returncode}" + (f": {why}" if why else "")
    line = (out or err or "").strip().splitlines()
    return True, redact_text(line[0])[0] if line else "ok"


async def _probe(key: str) -> dict:
    """Probe one oracle read-only. Never touches the breaker except to read it."""
    label = oracles.label(key)
    available = oracles.available(key)
    gate = breaker.snapshot(key)
    checks: list[dict] = []
    status: str
    fix: str | None = None

    # The operator's denylist is checked FIRST and reported as itself. `available()`
    # returns False for a disabled backend too, so a disabled oracle with its CLI
    # happily installed was reported as "`mmx` not on PATH — install the CLI", and a
    # disabled `fable` (whose row never consults `available`) read a cheerful "ok"
    # while every call returned `disabled`.
    if oracles.is_disabled(key):
        return {
            "key": key,
            "label": label,
            "status": "disabled",
            "resolved_model": label,
            "timeout_s": _timeout_for(key),
            "checks": [
                {"name": "enabled", "ok": False, "detail": "turned off by the operator"}
            ],
            "fix": (
                f're-enable with configure_disabled(enable=["{key}"]) '
                "or remove it from ASK_FABLE_DISABLED"
            ),
            "gate": {
                "state": gate["state"],
                "skip_reason": gate["skip_reason"],
                "resume_in_s": gate["resume_in_s"],
            },
        }

    if key in _CLI_BINARIES:
        binary = _CLI_BINARIES[key]
        if not available:
            status = "not_configured"
            # Distinguish "no binary" from "binary present but unusable" (kimi without
            # its config.toml): the old text told the operator to install a CLI they
            # already had.
            on_path = shutil.which(binary) is not None
            checks.append(
                {
                    "name": "cli",
                    "ok": False,
                    "detail": (
                        f"`{binary}` is on PATH but not usable yet (missing config or login)"
                        if on_path
                        else f"`{binary}` not on PATH"
                    ),
                }
            )
            fix = _FIX.get(key)
        else:
            ok, detail = await asyncio.to_thread(_cli_version, binary)
            checks.append({"name": "cli", "ok": ok, "detail": detail})
            if not ok:
                status = "error"  # on PATH but broken — configured-and-failing
                fix = _FIX.get(key, f"reinstall or re-login the `{binary}` CLI")
            else:
                status = "ok"
    elif key in _OAUTH_KEYS:
        # Claude Code, or the API transport when explicitly pinned. NEVER hit the
        # API here (token refresh has side effects) — this is a presence check, the
        # same one `oracles.available` uses, which is why a host with no Claude Code
        # no longer reports a cheerful "ok" it cannot honour.
        present = fable.claude_code_present()
        checks.append(
            {
                "name": "claude_code",
                "ok": present,
                "detail": (
                    "Claude Agent SDK or `claude` CLI on PATH"
                    if present
                    else "no importable claude_agent_sdk and no `claude` on PATH"
                ),
            }
        )
        if present:
            status = "ok"
        elif fable.http_transport_selected() and fable.http_api_key():
            checks.append(
                {"name": "http", "ok": True, "detail": "Anthropic API key configured"}
            )
            status = "ok"
        else:
            status = "not_configured"
            fix = _FIX_CLAUDE_CODE
    else:
        # HTTP bridge (glm/deepseek): key presence is the static reachability check.
        checks.append(
            {
                "name": "config",
                "ok": available,
                "detail": "configured" if available else "no API key",
            }
        )
        if not available:
            status = "not_configured"
            fix = _FIX.get(key, f"configure the {key} backend")
        else:
            status = "ok"

    # A reachable-but-degraded backend (breaker open/half-open, or a live quota
    # hold) is a warning, never ok — but never downgrades a not_configured/error.
    if status == "ok" and gate["skip_reason"] is not None:
        status = "warning"
        fix = fix or (
            f"circuit breaker open — recovers in ~{gate['resume_in_s']}s"
            if gate["skip_reason"] == "circuit_open"
            else f"rate-limited — resumes in ~{gate['resume_in_s']}s"
        )

    row = {
        "key": key,
        "label": label,
        "status": status,
        "resolved_model": label,
        "timeout_s": _timeout_for(key),
        "checks": checks,
        "gate": {
            "state": gate["state"],
            "skip_reason": gate["skip_reason"],
            "resume_in_s": gate["resume_in_s"],
        },
    }
    if fix:
        row["fix"] = fix
    return row


def _failed_row(key: str, status: str, detail: str, fix: str) -> dict:
    """The row for a probe that timed out or raised. Built defensively: the label or
    timeout lookup may be exactly what made the probe raise."""
    try:
        label: str = oracles.label(key)
    except Exception:  # noqa: BLE001 — one oracle's bad config must not sink the report
        label = key
    try:
        timeout_s: float | None = _timeout_for(key)
    except Exception:  # noqa: BLE001
        timeout_s = None
    return {
        "key": key,
        "label": label,
        "status": status,
        "resolved_model": label,
        "timeout_s": timeout_s,
        "checks": [{"name": "probe", "ok": False, "detail": detail}],
        "gate": {"state": "closed", "skip_reason": None, "resume_in_s": None},
        "fix": fix,
    }


def _rollup(rows: list[dict]) -> str:
    """error if any configured backend is broken; warning if any is degraded;
    else ok. not_configured and disabled backends do not affect the rollup
    (unconfigured — or deliberately turned off — is not a fault)."""
    statuses = {r["status"] for r in rows}
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    return "ok"


async def run(*, total_deadline: float = _TOTAL_DEADLINE_S) -> dict:
    """Probe every KNOWN oracle concurrently and roll up. Read-only; never records
    a breaker outcome or makes a model call.

    Each probe is individually bounded (the CLI ``--version`` has its own timeout in
    a killed process group), so the concurrent set returns in about one probe's
    time; ``total_deadline`` is a backstop that reports any still-unfinished probe
    as a timeout rather than hanging the tool. A probe that raises becomes an
    ``error`` row for its own oracle instead of aborting the whole report."""
    keys = list(oracles.KNOWN)
    tasks = [asyncio.ensure_future(_probe(k)) for k in keys]
    try:
        # return_exceptions: one probe raising must not discard every other row.
        await asyncio.wait_for(
            asyncio.shield(asyncio.gather(*tasks, return_exceptions=True)),
            timeout=total_deadline,
        )
    except TimeoutError:
        pass
    rows: list[dict] = []
    for key, task in zip(keys, tasks, strict=True):
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is None:
                rows.append(task.result())
                continue
            why = redact_text(f"{type(exc).__name__}: {exc}")[0][:300]
            rows.append(_failed_row(key, "error", f"probe failed: {why}",
                                    "the health probe itself crashed; see the detail"))
            continue
        task.cancel()
        rows.append(_failed_row(key, "warning", "probe timed out",
                                "probe exceeded the deadline; re-run diagnose"))
    rows.sort(key=lambda r: r["key"])
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "rollup": _rollup(rows),
        "summary": summary,
        "checked": len(rows),
        "oracles": rows,
    }
