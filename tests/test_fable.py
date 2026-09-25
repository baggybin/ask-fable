"""ask_fable Fable invocation — response shaping, CLI bridge, dispatch.

The SDK path is exercised via `run()` dispatch (with `_run_sdk` stubbed); the CLI
bridge is tested by mocking `subprocess.Popen`. No real model is ever called.
"""

from __future__ import annotations

import asyncio
import os

import pytest

import ask_fable.fable as fable
from ask_fable import cli_gate, isolation, oracle_common
from ask_fable.oracle_common import OracleResult


def _run(coro):
    return asyncio.run(coro)


def test_shape_ok_refused_empty():
    assert oracle_common.shape("The router dispatches to _handle.").status == "ok"
    r = oracle_common.shape("REFUSED: question is too broad")
    assert r.status == "refused" and r.text == "question is too broad"
    assert oracle_common.shape("REFUSED:").status == "refused"  # empty reason -> default
    assert oracle_common.shape("   ").status == "error" and oracle_common.shape("").kind == "sdk_error"


from conftest import FakePopen as _FakePopen


def _stub_gate(monkeypatch, seen: dict):
    """Record cli_gate acquisition without touching the real semaphores."""
    import contextlib

    @contextlib.contextmanager
    def fake_hold(name, timeout=None):
        seen["gate"] = name
        yield

    monkeypatch.setattr(cli_gate, "hold", fake_hold)


def test_cli_ok_and_argv(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        cli_gate.subprocess, "Popen",
        _FakePopen.factory(stdout="Handlers are registered in build_server()."),
    )
    _stub_gate(monkeypatch, seen)
    res = _run(fable.run("How are handlers registered?", "def build(): ...", use_cli=True))
    assert res.status == "ok" and "Handlers" in res.text
    proc = _FakePopen.instances.pop()
    argv = proc.argv
    assert "-p" in argv and fable.fable_model() in argv
    assert "--strict-mcp-config" in argv
    # tools disabled: the flag is present with an empty-string value
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    # question travels on stdin, not argv
    assert proc.stdin_data.startswith("QUESTION:")
    assert not any("QUESTION" in a for a in argv)
    # hardened spawn: own session so the group can be killed, and gated per-binary
    assert proc.kw.get("start_new_session") is True
    assert seen["gate"] == "claude"


def test_cli_refused(monkeypatch):
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(cli_gate.subprocess, "Popen", _FakePopen.factory(stdout="REFUSED: not about code"))
    res = _run(fable.run("what is the best language overall really", use_cli=True))
    assert res.status == "refused" and res.text == "not about code"


def test_cli_non_string_result_is_structured_error(monkeypatch):
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        cli_gate.subprocess, "Popen", _FakePopen.factory(stdout='{"result":{"nested":true}}')
    )

    res = _run(fable.run("How does dispatch work?", use_cli=True))

    assert res.status == "error" and res.kind == "sdk_error"


def test_cli_timeout_kills_process_group(monkeypatch):
    killed: dict = {}
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(cli_gate.subprocess, "Popen", _FakePopen.factory(timeout_first=True))
    monkeypatch.setattr(cli_gate.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli_gate.os, "killpg", lambda pgid, sig: killed.__setitem__("pgid", pgid))
    res = _run(fable.run("Does the parser handle nested fences here?", use_cli=True, timeout=1))
    assert res.status == "error" and res.kind == "timeout"
    assert "pgid" in killed  # the whole process group was SIGKILLed, not just the child


def test_cli_nonzero_surfaces_stderr(monkeypatch):
    """A failed claude CLI routes through cli_error_detail like every other CLI
    bridge — stderr is surfaced, not swallowed behind a constant string."""
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(cli_gate.subprocess, "Popen", _FakePopen.factory(returncode=1, stderr="kaboom"))
    res = _run(fable.run("Where is the router defined in this module?", use_cli=True))
    assert res.status == "error" and res.kind == "sdk_error"
    assert "kaboom" in res.text


def test_cli_usage_limit_is_rate_limit(monkeypatch):
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        cli_gate.subprocess,
        "Popen",
        _FakePopen.factory(returncode=1, stderr="usage limit reached — resets at 5pm"),
    )
    res = _run(fable.run("Where is the router defined in this module?", use_cli=True))
    assert res.status == "error" and res.kind == "rate_limit"
    assert "usage limit" in res.text


def test_cli_logged_out_is_auth_failed(monkeypatch):
    """An expired login must classify auth_failed (breaker-exempt) so the
    actionable message keeps surfacing instead of circuit_open."""
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        cli_gate.subprocess,
        "Popen",
        _FakePopen.factory(returncode=1, stderr="Not logged in. Please run /login"),
    )
    res = _run(fable.run("Where is the router defined in this module?", use_cli=True))
    assert res.status == "error" and res.kind == "auth_failed"
    assert "/login" in res.text


def test_cli_binary_missing(monkeypatch):
    called = {"popen": False}

    def mark(*a, **k):
        called["popen"] = True
        raise AssertionError("should not spawn")

    monkeypatch.setattr(fable.shutil, "which", lambda b: None)
    monkeypatch.setattr(cli_gate.subprocess, "Popen", mark)
    res = _run(fable.run("How does dispatch work in this file?", use_cli=True))
    assert res.status == "error" and res.kind == "binary_missing"
    assert called["popen"] is False  # never spawned


def test_run_dispatches_to_sdk_with_resume(monkeypatch):
    seen = {}

    async def fake_sdk(
        message, timeout, resume, system_prompt=None, on_think=None, spec=None, web_search=False
    ):
        seen["resume"] = resume
        seen["message"] = message
        seen["system_prompt"] = system_prompt
        seen["spec"] = spec
        seen["web_search"] = web_search
        return OracleResult("ok", text="ok", session_id="sid-2")

    monkeypatch.setattr(fable, "_run_sdk", fake_sdk)
    res = _run(fable.run("Trace the call path from run() to _handle.", resume="sid-1"))
    assert res.status == "ok" and res.session_id == "sid-2"
    assert seen["resume"] == "sid-1" and seen["message"].startswith("QUESTION:")
    # the model spec defaults to Fable, resolved through the ladder
    assert seen["spec"].key == "fable" and seen["spec"].model == fable.fable_model()
    # a normal ask turn is toolless — web search is off unless explicitly requested
    assert seen["web_search"] is False


def test_cli_provider_safeguard_is_refused(monkeypatch):
    """A provider-safeguard refusal on the CLI transport classifies as `refused`
    (kind provider_refusal), NOT as a generic sdk_error — so it stops inflating
    the error rate toward a breaker trip and reaches the caller as a refusal."""
    monkeypatch.setattr(fable.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(
        cli_gate.subprocess,
        "Popen",
        _FakePopen.factory(
            returncode=1,
            stderr="API Error: Opus 4.8's safeguards flagged this message.",
        ),
    )
    res = _run(fable.run("Where is the router defined in this module?", use_cli=True))
    assert res.status == "refused" and res.kind == "provider_refusal"


# --- SDK transport: provider refusal + max-turns salvage --------------------

def _install_fake_sdk(monkeypatch, messages: list):
    """Install a fake claude_agent_sdk whose client replays `messages` (a live
    list, so tests can append after install). Returns the message classes."""
    import sys
    import types

    class AssistantMessage:
        def __init__(self, content=None, error=None):
            self.content = content or []
            self.error = error

    class ResultMessage:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class _Options:
        def __init__(self, **kw):
            pass

    class _Client:
        def __init__(self, options=None):
            pass

        async def connect(self):
            pass

        async def query(self, message):
            pass

        async def receive_response(self):
            for m in messages:
                yield m

        async def disconnect(self):
            pass

    mod = types.ModuleType("claude_agent_sdk")
    mod.AssistantMessage = AssistantMessage
    mod.ResultMessage = ResultMessage
    mod.TextBlock = TextBlock
    mod.ClaudeAgentOptions = _Options
    mod.ClaudeSDKClient = _Client
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    monkeypatch.setattr(fable, "best_cli_path", lambda: None)
    return AssistantMessage, ResultMessage, TextBlock


def test_sdk_provider_safeguard_is_refused(monkeypatch):
    messages: list = []
    _AM, RM, _TB = _install_fake_sdk(monkeypatch, messages)
    messages.append(
        RM(
            is_error=True,
            result="API Error: Opus 4.8's safeguards flagged this message.",
            stop_reason="refusal",
            session_id="s1",
            model="claude-opus-5",
        )
    )
    res = _run(fable._run_sdk("msg", 60, None, "sys", None, fable.fable_spec(), False))
    assert res.status == "refused" and res.kind == "provider_refusal"
    assert res.telemetry is not None  # real telemetry kept, not a 0ms stub


def test_sdk_max_turns_salvages_partial(monkeypatch):
    """The turn-budget wall used to discard the whole turn; now any prose already
    emitted comes back as a never-cached truncated partial."""
    messages: list = []
    AM, RM, TB = _install_fake_sdk(monkeypatch, messages)
    messages.append(AM(content=[TB("partial findings gathered so far")]))
    messages.append(RM(is_error=True, subtype="error_max_turns", session_id="s2"))
    res = _run(fable._run_sdk("msg", 60, None, "sys", None, fable.fable_spec(), True))
    assert res.status == "ok" and res.kind == "truncated"
    assert "partial findings" in res.text
    assert res.meta.get("partial") is True and res.meta.get("stop_reason") == "max_turns"


def test_sdk_output_cap_stop_is_flagged_truncated(monkeypatch):
    # stop_reason "max_tokens" on a successful turn: the answer was cut off by the
    # output cap — returned, but flagged so no cache layer pins it as complete.
    messages: list = []
    AM, RM, TB = _install_fake_sdk(monkeypatch, messages)
    messages.append(AM(content=[TB("half of the answ")]))
    messages.append(RM(is_error=False, stop_reason="max_tokens", session_id="s9"))
    res = _run(fable._run_sdk("msg", 60, None, "sys", None, fable.fable_spec(), False))
    assert res.status == "ok" and res.kind == "truncated"
    assert res.meta.get("partial") is True and res.session_id == "s9"


def test_sdk_max_turns_without_prose_stays_error(monkeypatch):
    messages: list = []
    _AM, RM, _TB = _install_fake_sdk(monkeypatch, messages)
    messages.append(RM(is_error=True, subtype="error_max_turns", session_id="s3"))
    res = _run(fable._run_sdk("msg", 60, None, "sys", None, fable.fable_spec(), True))
    assert res.status == "error"


def _install_failing_sdk(monkeypatch):
    """A fake claude_agent_sdk whose client RAISES ``_Client.exc`` — a connection
    error from connect(), anything else mid-stream — the way the real SDK surfaces a
    missing binary or a CLI process that died. Returns the module."""
    import sys
    import types

    class ClaudeSDKError(Exception):
        pass

    class CLIConnectionError(ClaudeSDKError):
        pass

    class CLINotFoundError(CLIConnectionError):
        pass

    class _Client:
        exc: Exception | None = None
        stderr_lines: tuple[str, ...] = ()  # what the dying process wrote to stderr

        def __init__(self, options=None):
            self.options = options or {}

        async def connect(self):
            if isinstance(self.exc, CLIConnectionError):
                raise self.exc

        async def query(self, message):
            pass

        async def receive_response(self):
            for line in self.stderr_lines:
                self.options["stderr"](line)
            raise self.exc
            yield  # pragma: no cover — makes this an async generator

        async def disconnect(self):
            pass

    mod = types.ModuleType("claude_agent_sdk")
    for cls in (ClaudeSDKError, CLIConnectionError, CLINotFoundError):
        setattr(mod, cls.__name__, cls)
    mod.AssistantMessage = type("AssistantMessage", (), {})
    mod.ResultMessage = type("ResultMessage", (), {})
    mod.TextBlock = type("TextBlock", (), {})
    mod.ClaudeAgentOptions = lambda **kw: kw
    mod.ClaudeSDKClient = _Client
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    monkeypatch.setattr(fable, "best_cli_path", lambda: None)
    return mod


async def _no_cli(*a, **k):  # pragma: no cover — asserted by never being called
    raise AssertionError("the CLI transport must not be tried")


_EXIT_1 = "Command failed with exit code 1 (exit code: 1)"
_LOST = "No conversation found with session ID: sid-gone"


@pytest.mark.parametrize(
    "make_exc, stderr, resume, flagged",
    [
        # SDK 0.2.x re-raises a dead CLI process mid-stream as a plain Exception; the
        # reason is only on the process's stderr, which the stderr callback captures.
        (lambda sdk: Exception(_EXIT_1), (_LOST,), "sid-gone", True),
        # ...or inside the exception text itself (ProcessError appends stderr).
        (lambda sdk: Exception(f"{_EXIT_1}\nError output: {_LOST}"), (), "sid-gone", True),
        # A transient failure on a live session must NOT condemn the id: a 429, an
        # overload, a slow startup — forgetting the id would lose the whole thread.
        (lambda sdk: Exception(_EXIT_1), ("API Error: 529 overloaded",), "sid-1", False),
        (lambda sdk: Exception("Control request timeout: initialize"), (), "sid-1", False),
        (lambda sdk: Exception(_EXIT_1), (_LOST,), None, False),
        # Could not even start Claude Code: says nothing against the resume id.
        (lambda sdk: sdk.CLIConnectionError("Failed to start Claude Code: EACCES"), (), "sid-1",
         False),
    ],
)
def test_a_failed_sdk_process_is_an_error_result_not_an_exception(
    monkeypatch, make_exc, stderr, resume, flagged
):
    """The SDK RAISES when the Claude Code process fails, and _dispatch mapped only
    ImportError — so the exception escaped run() into the tool's catch-all (no audit
    row, no breaker record), and a dead resume id wedged its session until reset.
    Only a failure that names the lost conversation flags the id."""
    sdk = _install_failing_sdk(monkeypatch)
    sdk.ClaudeSDKClient.exc = make_exc(sdk)
    sdk.ClaudeSDKClient.stderr_lines = stderr
    monkeypatch.setattr(fable, "_run_cli", _no_cli)  # a transport answered: no re-route
    res = _run(fable.run("Why does this deadlock?", resume=resume))
    assert res.status == "error" and res.kind == "sdk_error"
    assert "Fable SDK request failed" in res.text and res.model == fable.fable_model("sdk")
    assert bool(res.meta.get("resume_failed")) is flagged


def test_an_sdk_with_no_claude_code_binary_steps_on_to_the_cli(monkeypatch):
    """CLINotFoundError is the SDK saying the transport is not THERE — the one case
    `auto` may step past. It escaped instead, so the CLI was never tried."""
    sdk = _install_failing_sdk(monkeypatch)
    sdk.ClaudeSDKClient.exc = sdk.CLINotFoundError("Claude Code not found")
    seen = {}

    async def fake_cli(message, timeout, system_prompt, spec, web_search=False, resume=None):
        seen["resume"] = resume
        return OracleResult("ok", text="answered by the CLI")

    monkeypatch.setattr(fable, "_run_cli", fake_cli)
    res = _run(fable.run("Why does this deadlock?", resume="sid-1"))
    assert res.status == "ok" and res.text == "answered by the CLI"
    assert seen["resume"] == "sid-1"  # the CLI continues the same conversation
    # Pinned to the SDK there is no rung to step to: the absence is the answer.
    monkeypatch.setenv(fable.TRANSPORT_ENV, "sdk")
    monkeypatch.setattr(fable, "_run_cli", _no_cli)
    res = _run(fable.run("Why does this deadlock?"))
    assert res.status == "error" and res.kind == "binary_missing"


def test_sdk_error_naming_a_lost_conversation_is_flagged(monkeypatch):
    messages: list = []
    _AM, RM, _TB = _install_fake_sdk(monkeypatch, messages)
    messages.append(RM(is_error=True, result="No conversation found with session ID: sid-gone"))
    res = _run(fable._run_sdk("msg", 60, "sid-gone", "sys", None, fable.fable_spec(), False))
    assert res.status == "error" and res.meta.get("resume_failed") is True


def _capture_cli(monkeypatch, *, returncode=0, stdout=None, stderr=""):
    seen: dict = {}

    async def fake_run_cli_async(argv, *, gate, timeout, input_text=None, env=None, cwd=None, **kw):
        seen["argv"] = argv
        out = stdout if stdout is not None else '{"result": "answer", "session_id": "sid-1"}'
        return cli_gate.CliRun(returncode, out, stderr, False)

    monkeypatch.setattr(fable.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(fable.cli_gate, "run_cli_async", fake_run_cli_async)
    return seen


def test_cli_follow_up_resumes_the_session(monkeypatch):
    """The CLI transport dropped `resume`: every follow-up ran `claude -p` as a fresh,
    memoryless thread that still reported ok — and its new id replaced the real one."""
    seen = _capture_cli(monkeypatch)
    res = _run(fable.run("And the error path?", resume="sid-1", use_cli=True))
    assert res.status == "ok" and res.session_id == "sid-1"
    argv = seen["argv"]
    assert argv[argv.index("--resume") + 1] == "sid-1"
    _run(fable.run("How does routing work?", use_cli=True))  # a first turn resumes nothing
    assert "--resume" not in seen["argv"]


def test_cli_dead_resume_id_is_flagged_but_other_failures_are_not(monkeypatch):
    lost = "No conversation found with session ID: sid-gone"
    _capture_cli(monkeypatch, returncode=1, stdout="", stderr=lost)
    res = _run(fable.run("And the error path?", resume="sid-gone", use_cli=True))
    assert res.status == "error" and res.meta.get("resume_failed") is True
    _capture_cli(monkeypatch, returncode=1, stdout="", stderr="usage limit reached")
    res = _run(fable.run("And the error path?", resume="sid-1", use_cli=True))
    assert res.kind == "rate_limit" and "resume_failed" not in res.meta


def test_run_falls_back_to_cli_on_sdk_importerror(monkeypatch):
    async def boom(*a, **k):
        raise ImportError("no sdk")

    async def fake_cli(message, timeout, system_prompt=None, spec=None, web_search=False,
                       resume=None):
        return OracleResult("ok", text="from-cli")

    monkeypatch.setattr(fable, "_run_sdk", boom)
    monkeypatch.setattr(fable, "_run_cli", fake_cli)
    res = _run(fable.run("How does the module route requests internally?"))
    assert res.status == "ok" and res.text == "from-cli"


# --- repo + harness isolation ----------------------------------------------
#
# Claude Code discovers CLAUDE.md/AGENTS.md from its cwd *upward*, keys auto-memory
# and session transcripts to a cwd-derived dir, and — with `tools` never set — shipped
# its FULL built-in tool schema in the system prompt. Measured on a real turn: a 109 KB
# prompt_snapshot (EnterWorktree/Read/Bash/…) + a 12 KB skill_listing, ~19k input
# tokens for an 82-char question; with the layers below it drops to ~1.7k. These tests
# pin each layer so the prompt can only ever contain what the caller passed.


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    """The state dir is isolated by conftest; kept as a named fixture for the
    isolation tests below so they read intent, not plumbing."""
    return tmp_path


def _install_capturing_sdk(monkeypatch) -> dict:
    """Fake claude_agent_sdk that records ClaudeAgentOptions kwargs and replays a
    one-line ok turn. Returns the recorded kwargs."""
    import sys
    import types

    captured: dict = {}

    class _AssistantMessage:
        def __init__(self, content=None, error=None):
            self.content = content or []
            self.error = error

    class _ResultMessage:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _TextBlock:
        def __init__(self, text):
            self.text = text

    class _Options:
        def __init__(self, **kw):
            captured.update(kw)

    class _Client:
        def __init__(self, options=None):
            self.options = options

        async def connect(self):
            pass

        async def query(self, message):
            pass

        async def receive_response(self):
            yield _AssistantMessage(content=[_TextBlock("ok")])
            yield _ResultMessage(
                is_error=False, result="ok", session_id="s1",
                model="claude-fable-5-1", usage={}, stop_reason="end_turn",
            )

        async def disconnect(self):
            pass

    mod = types.ModuleType("claude_agent_sdk")
    mod.AssistantMessage = _AssistantMessage
    mod.ResultMessage = _ResultMessage
    mod.TextBlock = _TextBlock
    mod.ClaudeAgentOptions = _Options
    mod.ClaudeSDKClient = _Client
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    monkeypatch.setattr(fable, "best_cli_path", lambda: None)
    return captured


def test_oracle_cwd_is_a_controlled_empty_dir(isolated_state):
    d = isolation.oracle_cwd()
    assert d.is_dir()
    assert list(d.iterdir()) == []  # nothing for discovery to find
    assert isolation.oracle_cwd() == d  # stable across turns


def test_oracle_cwd_is_private_even_under_a_loose_umask(isolated_state, tmp_path, monkeypatch):
    # A script outside the server's 0o077 umask must not leave the state dir it creates
    # world-readable: ensure_dir_secure makes every directory it creates 0700.
    import os
    import stat as _stat

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "fresh-state"))
    old = os.umask(0o022)
    try:
        d = isolation.oracle_cwd()
    finally:
        os.umask(old)
    for path in (d, d.parent):
        assert _stat.S_IMODE(path.stat().st_mode) == 0o700, path


def test_sdk_turn_is_isolated_from_repo_and_harness(isolated_state, monkeypatch):
    captured = _install_capturing_sdk(monkeypatch)

    res = _run(fable._run_sdk("msg", 60, None, "sys", None, fable.fable_spec(), False))
    assert res.status == "ok"
    assert captured["setting_sources"] == []  # no CLAUDE.md/settings sources
    assert captured["tools"] == []  # no built-in tool schema in the prompt
    assert captured["extra_args"] == {"safe-mode": None}
    assert captured["env"] == fable._ISOLATION_ENV  # auto-memory + skill listings off
    cwd = captured["cwd"]
    assert cwd == str(isolation.oracle_cwd())  # not the caller's project
    assert os.path.isdir(cwd) and os.listdir(cwd) == []


def test_sdk_websearch_keeps_its_two_tools(isolated_state, monkeypatch):
    """Isolation must not strip the opt-in search path: reasoning gets no tools,
    ask_websearch gets exactly WebSearch/WebFetch."""
    captured = _install_capturing_sdk(monkeypatch)
    _run(fable._run_sdk("msg", 60, None, "sys", None, fable.fable_spec(), True))
    assert captured["tools"] == ["WebSearch", "WebFetch"]


def test_cli_turn_is_isolated_from_repo_and_harness(isolated_state, monkeypatch):
    seen: dict = {}

    async def fake_run_cli_async(argv, *, gate, timeout, input_text=None, env=None, cwd=None, **kw):
        seen.update(argv=argv, env=env, cwd=cwd)
        return cli_gate.CliRun(0, '{"result": "ok", "model": "claude-fable-5-1"}', "", False)

    monkeypatch.setattr(fable.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(fable.cli_gate, "run_cli_async", fake_run_cli_async)

    res = _run(fable._run_cli("msg", 60, "sys", fable.fable_spec(), False))
    assert res.status == "ok"
    assert "--safe-mode" in seen["argv"]
    i = seen["argv"].index("--tools")
    assert seen["argv"][i + 1] == ""  # --tools "": no built-in tool schemas
    assert seen["env"] == fable._ISOLATION_ENV
    assert seen["cwd"] == str(isolation.oracle_cwd())
    assert os.listdir(seen["cwd"]) == []
