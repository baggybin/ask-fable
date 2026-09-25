"""Sandboxed execution for ask_falsify ``run:`` receipts.

Runs an untrusted, model-authored Python snippet under **bubblewrap** (``bwrap``): new
namespaces with NO network, a read-only system, a size-capped private tmpfs, a CLEARED
environment, and layered resource limits. A ``run`` receipt is the only one that
*manufactures* evidence rather than retrieving it — and the only one that executes model
output — so it is **DEFAULT OFF**: the whole path is inert unless the operator sets
``ASK_FABLE_ALLOW_RUN=1`` *and* ``bwrap`` is installed.

Reuses :mod:`cli_gate` for the spawn/kill/timeout/gate protocol (own session, group-SIGKILL
on timeout, bounded drain). Never raises for an expected failure — a refusal, crash, or
timeout comes back as a :class:`SandboxResult` status, not an exception.

Isolation layers (hardened after a 3-model isolation review):
- ``bwrap`` — ``--unshare-all`` (no network + private pid/ipc/uts/mount/cgroup), plus
  ``--unshare-user --disable-userns`` so the child can't re-enter a fresh user namespace.
  Read-only ``/usr``; NO ``/home``/``/root``/``/etc`` bound; ``--clearenv`` (no API keys);
  size-capped ``--tmpfs /tmp`` + ``/dev/shm`` so a snippet can't fill host RAM as page cache.
- ``systemd-run --user --scope`` (when available; probed once) — real RSS accounting via
  ``MemoryMax``, ``TasksMax`` (fork-bomb cap), ``CPUQuota``, ``RuntimeMaxSec``, and cleanup
  even if the parent dies. No daemon added; falls back to bare ``prlimit`` if absent.
- ``prlimit`` belt — ``--as`` (backstop), ``--nofile``, ``--fsize``, ``--core=0``, ``--cpu``
  (kernel SIGXCPU, independent of the wall-clock watchdog). No ``--nproc``: RLIMIT_NPROC counts
  every task the user owns host-wide (threads included), so on a busy desktop bwrap's own clone
  failed with EAGAIN. The pid namespace reaps the whole tree; ``TasksMax`` (with systemd) caps
  forks.
- wall-clock enforced OUTSIDE all of the above by :mod:`cli_gate`; output truncated per stream.

The memory story is RSS via ``MemoryMax`` where systemd is present; ``RLIMIT_AS`` is only a
coarse backstop (CPython/glibc reserve large virtual AS), which is why the thread/arena env
below keeps ``--as`` from false-failing. This is a boundary for *accident and casual misuse
behind an opt-in*, not a defense against a determined human adversary.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from . import cli_gate

_TIMEOUT_S = 8.0
_MEM_MB = 1024
_TMPFS_BYTES = 64 * 1024 * 1024
_SHM_BYTES = 16 * 1024 * 1024
_MAX_OUTPUT = 8000  # chars per stream returned to the caller


def _clip_stream(text: str, limit: int = _MAX_OUTPUT) -> str:
    """Trim a stream to ``limit`` chars keeping BOTH ends.

    Head-only truncation loses the end of a traceback, and the end is where Python names
    the exception. That is now load-bearing: `ask_verify` reads the final exception line
    to tell "the check failed" from "the snippet was broken", so a snippet that floods
    stderr before dying could otherwise have its `ModuleNotFoundError` cut off and be
    read as a genuine refutation."""
    text = text or ""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n[… {len(text) - limit} chars elided …]\n{text[-tail:]}"
_READ_CAP = 65536   # chars the runner RETAINS per stream (bounds memory under an output flood)

# Not PYTHON* vars, so `python3 -I` keeps them. They shrink glibc's per-thread virtual
# arenas and stop BLAS/OpenMP reserving a thread+arena per core — the real cause of
# RLIMIT_AS false-failures (per the isolation review), not CPython itself.
_HARDEN_ENV = {
    "MALLOC_ARENA_MAX": "2",
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
}


# A non-zero exit whose stderr shows the SANDBOX failed rather than the snippet: bwrap's
# and prlimit's own diagnostics, or EAGAIN from a process/thread limit. That is a setup or
# resource failure, never evidence about a claim, so it must not read as a failing run.
_SETUP_PREFIXES = ("bwrap: ", "prlimit: ")
_EAGAIN = "Resource temporarily unavailable"


@dataclass(slots=True)
class SandboxResult:
    """Outcome of one sandboxed run. ``status`` is the single source of truth:
    ``ok`` (exit 0) / ``fail`` (the snippet exited non-zero) / ``timeout`` / ``disabled``
    (opt-in off) / ``unavailable`` (bwrap missing, or the sandbox itself failed: a
    bwrap/prlimit setup error, EAGAIN, a failed spawn) / ``error`` (bad input)."""

    status: str
    returncode: int | None
    stdout: str
    stderr: str


def _flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """Code execution is OPT-IN — off unless the operator explicitly allows it."""
    return _flag("ASK_FABLE_ALLOW_RUN")


def available() -> bool:
    """True only when the sandbox binary is present; without it we never exec."""
    return shutil.which("bwrap") is not None


def _limits() -> tuple[float, int]:
    def _num(env: str, default, cast):
        try:
            return cast(os.environ.get(env) or default)
        except (TypeError, ValueError):
            return default
    return _num("ASK_FABLE_RUN_TIMEOUT_S", _TIMEOUT_S, float), _num("ASK_FABLE_RUN_MEM_MB", _MEM_MB, int)


@functools.cache
def _systemd_scope() -> bool:
    """One-time probe: can we put the run in a transient systemd *user* scope (real RSS +
    TasksMax + CPU accounting, no daemon added)? Cached for the process; falls back to bare
    prlimit when there's no user session bus."""
    if not (shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR")):
        return False
    try:
        r = subprocess.run(
            ["systemd-run", "--user", "--scope", "-q", "--collect", "-p", "RuntimeMaxSec=5",
             "--", "true"],
            capture_output=True, timeout=8, check=False,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _bwrap(code_path: str) -> list[str]:
    """The isolation layer: no net, read-only system, size-capped private tmp, cleared env,
    the snippet bound read-only at /tmp/_run.py, run in isolated+unbuffered python."""
    bwrap = shutil.which("bwrap") or "bwrap"
    argv = [
        bwrap,
        "--unshare-all", "--unshare-user", "--disable-userns",
        "--die-with-parent", "--new-session", "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/tmp",
    ]
    for k, v in _HARDEN_ENV.items():
        argv += ["--setenv", k, v]
    argv += [
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/bin", "/bin",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--proc", "/proc",
        "--dev", "/dev",
        "--size", str(_SHM_BYTES), "--tmpfs", "/dev/shm",
        "--size", str(_TMPFS_BYTES), "--tmpfs", "/tmp",
        "--ro-bind", code_path, "/tmp/_run.py",
        "--chdir", "/tmp",
    ]
    # A process cap INSIDE the namespace. The outer `prlimit --as` is per-process and
    # the pid namespace only guarantees reaping, so without systemd-run's TasksMax
    # (no `systemd-run`, or no XDG_RUNTIME_DIR — a headless host) nothing bounded
    # forks at all: `while True: os.fork()` ran for the whole wall clock with
    # host-wide effect. RLIMIT_NPROC has been counted per USER NAMESPACE since Linux
    # 5.14, so applying it under --unshare-user caps this sandbox and nothing else.
    argv += _inner_nproc() + ["python3", "-I", "-B", "-u", "/tmp/_run.py"]
    return argv


# Enough for the interpreter and any threads a snippet legitimately starts; far
# below what a fork bomb needs.
_INNER_NPROC = 64


def _inner_nproc() -> list[str]:
    """`prlimit --nproc` as seen from inside the sandbox, or nothing if the binary
    is not on the sandbox's PATH (/usr/bin:/bin, both bound read-only)."""
    for path in ("/usr/bin/prlimit", "/bin/prlimit"):
        if os.path.exists(path):
            return [path, f"--nproc={_INNER_NPROC}", "--"]
    return []


def _prlimit(inner: list[str], mem_mb: int, timeout: float) -> list[str]:
    prlimit = shutil.which("prlimit")
    if not prlimit:
        return inner
    return [
        prlimit,
        f"--as={mem_mb * 1024 * 1024}", "--nofile=128",
        f"--fsize={_TMPFS_BYTES}", "--core=0", f"--cpu={int(timeout) + 2}", "--",
        *inner,
    ]


def _systemd_wrap(inner: list[str], mem_mb: int, timeout: float) -> list[str]:
    return [
        "systemd-run", "--user", "--scope", "-q", "--collect",
        "-p", f"MemoryMax={mem_mb}M", "-p", "MemorySwapMax=0", "-p", "TasksMax=64",
        "-p", "CPUQuota=100%", "-p", f"RuntimeMaxSec={int(timeout) + 2}", "--",
        *inner,
    ]


def _argv(code_path: str, mem_mb: int, timeout: float) -> list[str]:
    """systemd-run(if available) → prlimit(if available) → bwrap → python. Each layer is a
    no-op fallthrough when its tool is missing; bwrap is the required floor."""
    argv = _prlimit(_bwrap(code_path), mem_mb, timeout)
    if _systemd_scope():
        argv = _systemd_wrap(argv, mem_mb, timeout)
    return argv


async def run_python(code: str) -> SandboxResult:
    """Execute ``code`` in the sandbox and return its outcome. Inert unless
    :func:`enabled` and :func:`available`; output is truncated per stream."""
    if not enabled():
        return SandboxResult("disabled", None, "", "code execution is off (set ASK_FABLE_ALLOW_RUN=1)")
    if not (code or "").strip():
        return SandboxResult("error", None, "", "empty snippet")
    if not available():
        return SandboxResult("unavailable", None, "", "sandbox unavailable (bwrap not installed)")

    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(code)
            tmp = f.name
        timeout, mem_mb = _limits()
        run = await cli_gate.run_cli_async(
            _argv(tmp, mem_mb, timeout), gate="falsify_run", timeout=timeout, max_output_bytes=_READ_CAP
        )
        out, err = _clip_stream(run.stdout or ""), _clip_stream(run.stderr or "")
        if run.timed_out:
            return SandboxResult("timeout", None, out, err or f"timed out after {timeout:.0f}s")
        stderr = run.stderr or ""
        if run.returncode is None or (
            run.returncode != 0
            and (stderr.lstrip().startswith(_SETUP_PREFIXES) or _EAGAIN in stderr)
        ):
            # No exit status of the snippet's own (the spawn failed), or the sandbox around
            # it failed: a non-verdict, so falsify can't count it as disproof.
            return SandboxResult("unavailable", run.returncode, out, err or "sandbox did not run")
        return SandboxResult("ok" if run.returncode == 0 else "fail", run.returncode, out, err)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


async def self_test() -> bool:
    """Run the two canonical cases through the REAL path (exit 0 and exit 3). Used to gate
    ``run:`` receipts: if an environment quirk (a missing bind, a broken cap) makes these
    misbehave, the caller must not let a sandbox failure masquerade as a claim refutation."""
    if not (enabled() and available()):
        return False
    ok = await run_python("print('selftest-ok')")
    bad = await run_python("raise SystemExit(3)")
    return (ok.status == "ok" and "selftest-ok" in ok.stdout
            and bad.status == "fail" and bad.returncode == 3)
