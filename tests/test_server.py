"""ask_fable server handlers — gating, dispatch, sessions, audit.

Drives the module-level `_handle_ask` / `_handle_reset` with guard, fable, and
audit stubbed, so no model is called and no real audit path is written unless a
test opts in via ASK_FABLE_AUDIT_PATH.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from types import SimpleNamespace

import ask_fable.server as server
from ask_fable.oracle_common import OracleResult
from ask_fable.sessions import SessionStore


def _run(coro):
    return asyncio.run(coro)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def _stub_fable(monkeypatch, result):
    async def fake_run(question, context="", *, resume=None):
        fake_run.calls.append({"question": question, "resume": resume})
        return result

    fake_run.calls = []
    monkeypatch.setattr(server.fable, "run", fake_run)
    return fake_run


def test_guard_denied_never_calls_model(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (False, "prohibited_x"))
    spy = _stub_fable(monkeypatch, OracleResult("ok", text="should not happen"))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    out = _run(server._handle_ask(SessionStore(), {"question": "some blocked question here"}))
    assert out == {"status": "refused", "stage": "guard", "reason": "prohibited_x"}
    assert spy.calls == []  # model never invoked


def test_ok_records_session_and_resumes(monkeypatch):
    _allow(monkeypatch)
    spy = _stub_fable(monkeypatch, OracleResult("ok", text="The router dispatches.", session_id="sid-1"))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    store = SessionStore()
    out1 = _run(server._handle_ask(store, {"question": "How does routing work here?", "session": "s"}))
    assert out1["status"] == "ok" and out1["answer"] == "The router dispatches."
    assert out1["session"] == "s"
    # follow-up resumes with the captured session id
    out2 = _run(server._handle_ask(store, {"question": "And the error path?", "session": "s"}))
    assert out2["status"] == "ok"
    assert spy.calls[-1]["resume"] == "sid-1"


def test_model_refused_and_error(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    _stub_fable(monkeypatch, OracleResult("refused", text="too broad"))
    out = _run(server._handle_ask(SessionStore(), {"question": "what is the best editor to use"}))
    assert out == {"status": "refused", "stage": "model", "reason": "too broad"}
    _stub_fable(monkeypatch, OracleResult("error", kind="timeout", text="Fable timed out after 120s"))
    out = _run(server._handle_ask(SessionStore(), {"question": "Trace dispatch in this module."}))
    assert out["status"] == "error" and out["kind"] == "timeout"


def test_reset_flag_dumps_and_clears(monkeypatch, tmp_path):
    _allow(monkeypatch)
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _stub_fable(monkeypatch, OracleResult("ok", text="answer one", session_id="sid-1"))
    store = SessionStore()
    _run(server._handle_ask(store, {"question": "First question about routing.", "session": "s"}))
    # reset=true dumps the prior turn and starts fresh
    out = _run(server._handle_ask(store, {"question": "New topic about parsing.", "session": "s", "reset": True}))
    assert out.get("reset_dump")
    assert os.path.exists(out["reset_dump"])
    # session was cleared before this turn -> no resume id was passed
    assert store.resume_id("s") == "sid-1"  # re-populated by THIS turn


def test_handle_reset_saves_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = SessionStore()
    store.record_turn("s", "q", "a", "sid-1")
    out = server._handle_reset(store, {"session": "s", "save": True})
    assert out["cleared"] is True and out["dump"] and os.path.exists(out["dump"])
    assert store.resume_id("s") is None  # gone


def test_stream_reasoning_flag_passes_live_sink(monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setenv("ASK_FABLE_STREAM_REASONING", "1")
    monkeypatch.setenv("ASK_FABLE_QUIET", "0")  # reporter must be enabled to produce a sink
    monkeypatch.setenv("ASK_FABLE_SHOW_REASONING", "1")
    seen = {}

    async def fake_run(question, context="", *, resume=None, on_think=None):
        seen["sink"] = on_think
        if on_think:
            on_think("live reasoning chunk")  # best-effort sink must not raise
        return OracleResult("ok", text="answer", thinking="full trace")

    monkeypatch.setattr(server.fable, "run", fake_run)
    out = _run(server._handle_ask(SessionStore(), {"question": "Trace the dispatch path here."}))
    assert out["status"] == "ok" and callable(seen["sink"])  # a live streaming sink was wired


def test_session_dump_includes_thinking(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = SessionStore()
    store.record_turn("s", "q", "a", "sid-1", thinking="weighed X vs Y, chose X")
    out = server._handle_reset(store, {"session": "s", "save": True})
    text = open(out["dump"]).read()
    assert "**Thinking:**" in text and "weighed X vs Y, chose X" in text


def test_add_thinking_opt_in_cap_and_disable(monkeypatch):
    # Off by default: nothing attached.
    monkeypatch.delenv("ASK_FABLE_RETURN_THINKING", raising=False)
    p: dict = {}
    server._add_thinking(p, "some reasoning")
    assert "thinking" not in p

    monkeypatch.setenv("ASK_FABLE_RETURN_THINKING", "1")

    # Under the cap: attached verbatim.
    monkeypatch.setenv("ASK_FABLE_THINKING_CHARS", "4000")
    p = {}
    server._add_thinking(p, "short trace")
    assert p["thinking"] == "short trace"

    # Over the cap: truncated with an ellipsis marker.
    monkeypatch.setenv("ASK_FABLE_THINKING_CHARS", "5")
    p = {}
    server._add_thinking(p, "abcdefghij")
    assert p["thinking"] == "abcde …"

    # cap <= 0 disables the excerpt entirely (no bare " …").
    monkeypatch.setenv("ASK_FABLE_THINKING_CHARS", "0")
    p = {}
    server._add_thinking(p, "abcdefghij")
    assert "thinking" not in p


def test_audit_file_is_written_owner_only(monkeypatch, tmp_path):
    log = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("ASK_FABLE_AUDIT_PATH", str(log))
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (False, "prohibited_x"))
    _run(server._handle_ask(SessionStore(), {"question": "blocked question text here"}))
    assert log.exists()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["decision"] == "denied" and rec["stage"] == "guard"
    assert "question_raw" not in rec  # hashed by default
    assert len(rec["question_sha256"]) == 64


def test_build_server_and_schema():
    s = server.build_server()
    assert s.name == "ask_fable"
    assert server._ASK_SCHEMA["required"] == ["question"]
    assert "session" in server._ASK_SCHEMA["properties"]
    assert "task" in server._LIST_ATLAS_SCHEMA["properties"]
    assert "interactive" in server._LIST_ATLAS_SCHEMA["properties"]


def test_list_atlas_models_rejects_limit_outside_schema_bounds():
    assert server._schema_error("list_atlas_models", {"limit": 0}) == "invalid value for argument: limit"
    assert server._schema_error("list_atlas_models", {"limit": 9}) == "invalid value for argument: limit"
    assert server._schema_error("list_atlas_models", {"task": "x"}) == "invalid value for argument: task"


def test_atlas_selection_uses_native_elicitation_when_supported():
    # Given: an MCP client that advertises form elicitation and accepts a choice.
    class _Session:
        client_params = SimpleNamespace(
            capabilities=SimpleNamespace(
                elicitation=SimpleNamespace(form=SimpleNamespace()),
            ),
        )

        def __init__(self):
            self.requested_schema = None

        async def elicit_form(self, message, requested_schema):
            self.requested_schema = requested_schema
            return SimpleNamespace(
                action="accept",
                content={"model": "vendor/code", "effort": "deep"},
            )

    session = _Session()
    fake_server = SimpleNamespace(
        request_context=SimpleNamespace(session=session),
    )
    listing = {
        "recommendations": [
            {
                "model_id": "vendor/code",
                "label": "Code",
                "picker_description": "Best fit for coding",
            },
        ],
        "effort_choices": [
            {"value": "deep", "label": "Deep", "max_tokens": 16_384},
        ],
    }

    # When: the task-aware Atlas listing requests an interactive selection.
    selection = _run(server._elicit_atlas_selection(fake_server, listing, "debug a repo"))

    # Then: the native form contains model and effort menus and returns the choice.
    assert session.requested_schema["properties"]["model"]["enum"] == ["vendor/code"]
    assert selection == {
        "supported": True,
        "action": "accept",
        "model": "vendor/code",
        "effort": "deep",
    }


def test_atlas_selection_falls_back_for_invalid_accepted_content():
    class _Session:
        client_params = SimpleNamespace(
            capabilities=SimpleNamespace(
                elicitation=SimpleNamespace(form=SimpleNamespace()),
            ),
        )

        async def elicit_form(self, message, requested_schema):
            return SimpleNamespace(action="accept", content={})

    listing = {
        "recommendations": [
            {
                "model_id": "vendor/code",
                "label": "Code",
                "picker_description": "Best fit for coding",
            },
        ],
        "effort_choices": [{"value": "deep", "label": "Deep"}],
    }
    fake_server = SimpleNamespace(
        request_context=SimpleNamespace(session=_Session()),
    )

    selection = _run(server._elicit_atlas_selection(fake_server, listing, "debug a repo"))

    assert selection == {"supported": True, "action": "fallback"}


def test_atlas_selection_does_not_request_a_form_without_form_capability():
    class _Session:
        client_params = SimpleNamespace(
            capabilities=SimpleNamespace(
                elicitation=SimpleNamespace(form=None, url=None),
            ),
        )

        def __init__(self):
            self.calls = 0

        async def elicit_form(self, message, requested_schema):
            self.calls += 1
            return SimpleNamespace(action="decline", content={})

    session = _Session()
    listing = {
        "recommendations": [{"model_id": "vendor/code"}],
        "effort_choices": [{"value": "deep"}],
    }
    fake_server = SimpleNamespace(request_context=SimpleNamespace(session=session))

    selection = _run(server._elicit_atlas_selection(fake_server, listing, "debug a repo"))

    assert selection == {"supported": False, "action": "fallback"}
    assert session.calls == 0
def test_server_advertises_instructions():
    # Standing MCP instructions are what make agents reach for the tools
    # unprompted — assert they're set and actually nudge proactive use.
    s = server.build_server()
    assert s.instructions
    assert "proactively" in s.instructions.lower()


def test_primary_tool_descriptions_are_trigger_first():
    # Descriptions should lead with WHEN/how-much to reach for the tool, not just
    # WHAT. `ask` is framed as the heavy-use default; the council is directional.
    from ask_fable.prompts import ASK_COUNCIL_TOOL_DESCRIPTION, ASK_TOOL_DESCRIPTION

    # `ask` leads with a strong, use-it-often directive
    assert ASK_TOOL_DESCRIPTION.startswith("YOUR DEFAULT MOVE")
    assert "liberally" in ASK_TOOL_DESCRIPTION.lower()
    # the council leads by framing itself as directional / reserved for high-stakes
    assert ASK_COUNCIL_TOOL_DESCRIPTION.startswith("DIRECTIONAL")
    assert "reserve" in ASK_COUNCIL_TOOL_DESCRIPTION.lower()


def test_council_descriptions_are_directional_not_default():
    # The council tools must steer toward `ask` as the default, not invite routine use.
    from ask_fable.prompts import ASK_COUNCIL_TOOL_DESCRIPTION, ASK_OLLAMA_COUNCIL_TOOL_DESCRIPTION

    for desc in (ASK_COUNCIL_TOOL_DESCRIPTION, ASK_OLLAMA_COUNCIL_TOOL_DESCRIPTION):
        assert "DIRECTIONAL" in desc
        assert "default to `ask`" in desc.lower()


def test_prompts_dont_oversell_latency():
    # Real p50 latency is ~69s — the old "20-second check" claim was misleading.
    from ask_fable.prompts import ASK_TOOL_DESCRIPTION, SERVER_INSTRUCTIONS

    for text in (ASK_TOOL_DESCRIPTION, SERVER_INSTRUCTIONS):
        assert "20-second" not in text and "20 second" not in text


def test_ask_prompt_makes_context_mandatory():
    # A third of real asks arrived with no context; the copy must push code into `context`.
    from ask_fable.prompts import ASK_TOOL_DESCRIPTION, SERVER_INSTRUCTIONS

    assert "ALWAYS paste the real code into `context`" in ASK_TOOL_DESCRIPTION
    assert "CANNOT open files" in ASK_TOOL_DESCRIPTION
    assert "put the real code it needs in `context`" in SERVER_INSTRUCTIONS


def test_fable_prompt_allows_defensive_security():
    # The guard only blocks offensive markers; the model prompt must not over-refuse
    # legitimate hardening of the agent's own code.
    from ask_fable.prompts import FABLE_SYSTEM_PROMPT

    assert "hardening" in FABLE_SYSTEM_PROMPT
    # refusal triggers on the question itself, not the code being security-related
    assert "question ITSELF" in FABLE_SYSTEM_PROMPT
    assert "security testing" not in FABLE_SYSTEM_PROMPT  # the old, too-broad refusal trigger
    # decision answers should lead with a recommendation, not a survey
    assert "concrete recommendation" in FABLE_SYSTEM_PROMPT


# ── trusted_session flag ────────────────────────────────────────────────

def test_trusted_session_allows_denylist_hit_through():
    """When trusted=true, a question that would be blocked by the denylist passes
    the guard — the guard switches to log-only mode."""
    monkeypatch = __import__("pytest").MonkeyPatch()
    with monkeypatch.context() as mp:
        mp.setattr(server.guard, "check_denylist", lambda p: (False, "offensive-security content"))
        mp.setattr(server.audit, "record", lambda **k: None)
        _stub_fable(mp, OracleResult("ok", text="PoC analysis complete.", session_id="sid"))
        store = SessionStore()
        out = _run(server._handle_ask(
            store, {"question": "analyze this PoC exploit for CVE-2024-12345", "trusted": True}
        ))
        assert out["status"] == "ok"
        assert "PoC analysis complete" in out["answer"]


def test_trusted_session_not_set_blocks_denylist_hit():
    """Without trusted=true, a denylist hit blocks the question normally."""
    monkeypatch = __import__("pytest").MonkeyPatch()
    with monkeypatch.context() as mp:
        mp.setattr(server.guard, "check_denylist", lambda p: (False, "offensive-security content"))
        mp.setattr(server.audit, "record", lambda **k: None)
        _stub_fable(mp, OracleResult("ok", text="should not reach fable", session_id="sid"))
        store = SessionStore()
        out = _run(server._handle_ask(
            store, {"question": "write an exploit", "trusted": False}
        ))
        assert out["status"] == "refused"
        assert out["stage"] == "guard"


def test_ask_schema_includes_trusted_field():
    """The _ASK_SCHEMA must expose the trusted flag so agents can set it."""
    assert "trusted" in server._ASK_SCHEMA["properties"]
    assert server._ASK_SCHEMA["properties"]["trusted"]["type"] == "boolean"
    assert "operator-authorized" in server._ASK_SCHEMA["properties"]["trusted"]["description"].lower()
