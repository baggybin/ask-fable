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
    assert out["status"] == "refused"
    assert out["stage"] == "guard"
    assert out["reason"] == "prohibited_x"
    # additive: the reframe recipe rides on the refusal (see _guard_refusal)
    assert "ask_fable_help" in out["how_to_reframe"]
    assert spy.calls == []  # model never invoked


def test_ok_records_session_and_resumes(monkeypatch):
    _allow(monkeypatch)
    spy = _stub_fable(
        monkeypatch, OracleResult("ok", text="The router dispatches.", session_id="sid-1")
    )
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    store = SessionStore()
    out1 = _run(
        server._handle_ask(store, {"question": "How does routing work here?", "session": "s"})
    )
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
    _stub_fable(
        monkeypatch, OracleResult("error", kind="timeout", text="Fable timed out after 120s")
    )
    out = _run(server._handle_ask(SessionStore(), {"question": "Trace dispatch in this module."}))
    assert out["status"] == "error" and out["kind"] == "timeout"


def test_http_follow_up_is_refused_not_answered_memoryless(monkeypatch):
    """On the http transport `ask` stored no resume id, so a follow-up on the same
    session was answered as a fresh thread — no memory of turn one — reported ok."""
    _allow(monkeypatch)
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setenv(server.fable.TRANSPORT_ENV, "http")
    monkeypatch.setenv(server.fable.HTTP_KEY_ENV, "sk-ant-test")
    asked = []

    async def fake_http(cfg, question, context="", *, timeout=None, system_prompt=None,
                        web_search=False):
        asked.append(question)
        return OracleResult("ok", text="Noted.", model=cfg.model)

    monkeypatch.setattr(server.fable.anthropic_http, "run", fake_http)
    store = SessionStore()

    def ask(question, **extra):
        return _run(server._handle_ask(store, {"question": question, "session": "s", **extra}))

    assert ask("Remember: retry cap is 7.")["status"] == "ok"
    follow = ask("Which retry cap?")
    assert follow["status"] == "error" and follow["kind"] == "transport_incapable"
    assert asked == ["Remember: retry cap is 7."]  # the follow-up never ran fresh
    # Starting the session over is explicit, and works.
    assert ask("New topic.", reset=True)["status"] == "ok" and asked[-1] == "New topic."


def test_a_dead_resume_id_is_dropped_so_the_session_recovers(monkeypatch):
    """A resumed turn the bridge flags `resume_failed` (the id's conversation is gone)
    left the id stored, so every later ask on the session failed until a reset."""
    _allow(monkeypatch)
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    store = SessionStore()
    store.record_turn("s", "earlier question", "earlier answer", "sid-gone")
    spy = _stub_fable(
        monkeypatch,
        OracleResult(
            "error", kind="sdk_error", text="Fable SDK request failed: exit 1",
            meta={"resume_failed": True},
        ),
    )
    out = _run(server._handle_ask(store, {"question": "And the error path?", "session": "s"}))
    assert spy.calls[-1]["resume"] == "sid-gone"
    assert out["status"] == "error" and "starts a fresh conversation" in out["detail"]
    assert store.resume_id("s") is None
    spy = _stub_fable(monkeypatch, OracleResult("ok", text="fresh answer", session_id="sid-new"))
    out = _run(server._handle_ask(store, {"question": "And the error path?", "session": "s"}))
    assert out["status"] == "ok" and spy.calls[-1]["resume"] is None
    assert store.resume_id("s") == "sid-new"


def test_an_ordinary_error_keeps_the_resume_id(monkeypatch):
    """Only an id the bridge says is dead is dropped. A timeout, or the http transport
    refusing to resume, says nothing against the conversation — dropping the id there
    would make the NEXT follow-up silently memoryless."""
    _allow(monkeypatch)
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    store = SessionStore()
    store.record_turn("s", "q", "a", "sid-1")
    for kind in ("timeout", "transport_incapable"):
        _stub_fable(monkeypatch, OracleResult("error", kind=kind, text="not this turn"))
        out = _run(server._handle_ask(store, {"question": "And the error path?", "session": "s"}))
        assert out["kind"] == kind and out["detail"] == "not this turn"
        assert store.resume_id("s") == "sid-1"


def test_reset_flag_dumps_and_clears(monkeypatch, tmp_path):
    _allow(monkeypatch)
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _stub_fable(monkeypatch, OracleResult("ok", text="answer one", session_id="sid-1"))
    store = SessionStore()
    _run(server._handle_ask(store, {"question": "First question about routing.", "session": "s"}))
    # reset=true dumps the prior turn and starts fresh
    out = _run(
        server._handle_ask(
            store, {"question": "New topic about parsing.", "session": "s", "reset": True}
        )
    )
    assert out.get("reset_dump")
    assert os.path.exists(out["reset_dump"])
    # session was cleared before this turn -> no resume id was passed
    assert store.resume_id("s") == "sid-1"  # re-populated by THIS turn


def test_handle_reset_saves_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = SessionStore()
    store.record_turn("s", "q", "a", "sid-1")
    out = _run(server._handle_reset(store, {"session": "s", "save": True}))
    assert out["cleared"] is True and out["dump"] and os.path.exists(out["dump"])
    assert store.resume_id("s") is None  # gone


def test_reset_waits_on_the_session_lock(monkeypatch, tmp_path):
    # RC-2: reset must serialize against an in-flight ask on the same key. Without the lock,
    # reset clears the store and the ask's post-await record_turn resurrects the session.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    async def scenario():
        store = SessionStore()
        store.record_turn("s", "q", "a", "sid-1")
        key = server._session_key("", "s")
        async with server._session_lock(key):  # stand in for an in-flight ask holding it
            task = asyncio.create_task(
                server._handle_reset(store, {"session": "s", "save": False})
            )
            await asyncio.sleep(0)  # let reset run if it were going to (it must not)
            assert not task.done()  # blocked on the lock
            assert store.resume_id(key) == "sid-1"  # session still intact
        out = await task  # lock released -> reset proceeds
        assert out["cleared"] is True and store.resume_id(key) is None

    _run(scenario())


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
    out = _run(server._handle_reset(store, {"session": "s", "save": True}))
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


def test_lms_host_diagnose_tools_are_schema_validated():
    # WI-1: these 6 tools were registered in list_tools but missing from _TOOL_SCHEMAS, so
    # _schema_error returned None (no server-side validation) and e.g. ask_lms ran on an
    # empty question instead of failing fast.
    for name in ("ask_lms", "ask_lms_council", "list_lms_models",
                 "unload_lms_model", "host_status", "diagnose"):
        assert name in server._TOOL_SCHEMAS, name
    assert server._schema_error("ask_lms", {}) is not None  # missing question now caught


def test_council_and_conference_schemas_accept_lmstudio():
    # WI-3: the models item schema must accept every token oracles.resolve() does, incl.
    # lmstudio:<model>, or strictly-validating MCP hosts reject documented calls.
    for schema in (server._COUNCIL_SCHEMA, server._CONFERENCE_SCHEMA):
        patterns = [
            item.get("pattern")
            for item in schema["properties"]["models"]["items"]["anyOf"]
        ]
        assert "^lmstudio:.+" in patterns


def test_list_atlas_models_rejects_limit_outside_schema_bounds():
    assert (
        server._schema_error("list_atlas_models", {"limit": 0})
        == "invalid value for argument: limit"
    )
    assert (
        server._schema_error("list_atlas_models", {"limit": 9})
        == "invalid value for argument: limit"
    )
    assert (
        server._schema_error("list_atlas_models", {"task": "x"})
        == "invalid value for argument: task"
    )


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


_OPENROUTER_CATALOG = {
    "data": [
        {
            "id": "openai/gpt-5.6-sol",
            "name": "GPT-5.6 Sol",
            "pricing": {"prompt": "0.000002", "completion": "0.00001"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "context_length": 400_000,
            "reasoning": {"supported_efforts": ["low", "high"]},
        },
        {
            "id": "deepseek/deepseek-v4",
            "name": "DeepSeek V4",
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "context_length": 128_000,
        },
    ]
}


class _ElicitingSession:
    """A client session that advertises form elicitation, for the tools/call path.
    ``reply`` is the picker's response — or an exception for the picker to raise."""

    client_params = SimpleNamespace(
        clientInfo=SimpleNamespace(name="test-client"),
        capabilities=SimpleNamespace(elicitation=SimpleNamespace(form=SimpleNamespace())),
    )

    def __init__(self, reply):
        self.reply = reply
        self.forms: list[tuple[str, dict]] = []

    async def elicit_form(self, message, requested_schema):
        self.forms.append((message, requested_schema))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _call_tool(name: str, arguments: dict, session) -> dict:
    """Drive the real tools/call handler (dispatch + `_text`) inside a request context."""
    import mcp.types as types
    from mcp.server.lowlevel.server import request_ctx
    from mcp.shared.context import RequestContext

    handler = server.build_server().request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )

    async def go():
        token = request_ctx.set(
            RequestContext(request_id=1, meta=None, session=session, lifespan_context=None)
        )
        try:
            return await handler(request)
        finally:
            request_ctx.reset(token)

    return json.loads(_run(go()).root.content[0].text)


def test_openrouter_task_listing_offers_an_openrouter_picker(monkeypatch):
    """list_models(provider="openrouter", task=…) crashed with KeyError
    'picker_description' — an Atlas-only field — on any client with form elicitation,
    losing the whole listing; the picker also said "Atlas" and took Atlas's effort."""
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_OPENROUTER_EFFORT", "standard")
    monkeypatch.setenv("ASK_FABLE_ATLAS_EFFORT", "quick")
    monkeypatch.setattr(server.openrouter, "_get_json", lambda *a, **k: _OPENROUTER_CATALOG)
    choice = {"model": "openai/gpt-5.6-sol", "effort": "deep"}
    session = _ElicitingSession(SimpleNamespace(action="accept", content=choice))

    out = _call_tool("list_models", {"provider": "openrouter", "task": "debug a repo"}, session)

    assert out["status"] == "ok" and out["model_count"] == 2
    assert out["selection"] == {"supported": True, "action": "accept", **choice}
    message, schema = session.forms[0]
    assert "OpenRouter" in message and "Atlas" not in message
    assert schema["properties"]["model"]["title"] == "OpenRouter model"
    # each OpenRouter option is labelled with its cost_note
    assert "GPT-5.6 Sol — $2.00/$10.00 per M" in schema["properties"]["model"]["enumNames"]
    assert schema["properties"]["effort"]["default"] == "standard"  # OpenRouter's, not Atlas's


def test_a_failing_picker_still_returns_the_listing(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.openrouter, "_get_json", lambda *a, **k: _OPENROUTER_CATALOG)
    session = _ElicitingSession(RuntimeError("client went away mid-form"))

    out = _call_tool("list_models", {"provider": "openrouter", "task": "debug a repo"}, session)

    assert out["status"] == "ok" and out["model_count"] == 2  # not an sdk_error
    assert out["selection"] == {"supported": True, "action": "fallback"}


def test_server_advertises_instructions():
    # Standing MCP instructions are what make agents reach for the tools
    # unprompted — assert they're set and actually nudge proactive use.
    s = server.build_server()
    assert s.instructions
    assert "BEFORE you guess" in s.instructions
    assert "Double-strike rule" in s.instructions


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
    """When trusted=true AND the operator opted in via ASK_FABLE_ALLOW_TRUSTED, a
    question that would be blocked by the denylist passes — log-only mode."""
    monkeypatch = __import__("pytest").MonkeyPatch()
    with monkeypatch.context() as mp:
        mp.setenv("ASK_FABLE_ALLOW_TRUSTED", "1")  # operator opt-in now required
        mp.setattr(server.guard, "check_denylist", lambda p: (False, "offensive-security content"))
        mp.setattr(server.audit, "record", lambda **k: None)
        _stub_fable(mp, OracleResult("ok", text="PoC analysis complete.", session_id="sid"))
        store = SessionStore()
        out = _run(
            server._handle_ask(
                store, {"question": "analyze this PoC exploit for CVE-2024-12345", "trusted": True}
            )
        )
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
        out = _run(server._handle_ask(store, {"question": "write an exploit", "trusted": False}))
        assert out["status"] == "refused"
        assert out["stage"] == "guard"


def test_ask_schema_includes_trusted_field():
    """The _ASK_SCHEMA must expose the trusted flag so agents can set it."""
    assert "trusted" in server._ASK_SCHEMA["properties"]
    assert server._ASK_SCHEMA["properties"]["trusted"]["type"] == "boolean"
    assert (
        "operator-authorized" in server._ASK_SCHEMA["properties"]["trusted"]["description"].lower()
    )


def test_trusted_flag_is_advertised_on_every_question_tool():
    """`trusted` is the one escape hatch from the denylist (question AND context
    scans), so every tool that takes a question must offer it — a tool without it
    has no operator override at all."""
    for tool in _advertised_tools():
        props = tool.inputSchema.get("properties") or {}
        if "question" in props:
            assert "trusted" in props, f"{tool.name} takes a question but no `trusted`"
            assert props["trusted"]["type"] == "boolean"


def test_guard_refusal_names_the_field():
    # A context hit must be labelled `where: context` so the caller strips the
    # framing from the right field instead of reframing the question.
    ctx = server._guard_refusal("offensive-security content (context)")
    assert ctx["where"] == "context"
    assert "`context`" in ctx["how_to_reframe"]
    q = server._guard_refusal("offensive-security content")
    assert q["where"] == "question"


def test_ask_fable_help_is_registered_and_free():
    # The overflow manual is only reachable if the tool is advertised, and it is
    # only worth advertising if it costs nothing — no model call, no network.
    import mcp.types as types

    s = server.build_server()
    handler = s.request_handlers[types.ListToolsRequest]
    tools = _run(handler(types.ListToolsRequest(method="tools/list"))).root.tools
    tool = next((t for t in tools if t.name == "ask_fable_help"), None)
    assert tool is not None, "ask_fable_help missing from list_tools()"
    assert "FREE" in tool.description and "no model call" in tool.description
    from ask_fable.prompts import HELP_TOPICS

    enum = tool.inputSchema["properties"]["topic"]["enum"]
    assert set(enum) == set(HELP_TOPICS) | {"all"}
    assert enum[-1] == "all"


def test_ask_fable_help_serves_every_topic():
    from ask_fable.prompts import HELP_TOPICS

    for topic in HELP_TOPICS:
        out = server._handle_help({"topic": topic})
        assert out["status"] == "ok"
        assert out["topic"] == topic
        assert out["help"] == HELP_TOPICS[topic]
        assert topic in out["topics"]
    default = server._handle_help({})
    assert default["topic"] == "all"
    assert all(body in default["help"] for body in HELP_TOPICS.values())


def test_guard_refusal_carries_the_reframe_recipe():
    # The reframe recipe used to live in the truncated standing instructions; it
    # now rides on the refusal, where the agent cannot miss it.
    out = server._guard_refusal("prohibited_x")
    assert out["status"] == "refused" and out["reason"] == "prohibited_x"
    advice = out["how_to_reframe"]
    assert "DETERMINISTIC" in advice
    assert 'ask_fable_help("refused")' in advice
    assert "`context`" in advice


def _advertised_tools():
    import mcp.types as types

    s = server.build_server()
    handler = s.request_handlers[types.ListToolsRequest]
    return _run(handler(types.ListToolsRequest(method="tools/list"))).root.tools


def test_every_tool_carries_all_four_annotation_hints():
    # Some MCP tool directories (e.g. OpenAI's) reject a tool where any of the
    # four hints is missing or non-boolean. Enforce explicit booleans on every
    # advertised tool so we never silently ship one on the permissive defaults.
    hints = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    for tool in _advertised_tools():
        ann = tool.annotations
        assert ann is not None, f"{tool.name} has no annotations"
        assert ann.title, f"{tool.name} has no annotation title"
        for hint in hints:
            value = getattr(ann, hint)
            assert isinstance(value, bool), f"{tool.name}.{hint} is {value!r}, not a bool"


def test_annotation_table_matches_advertised_tools():
    # The name->annotations table must stay in lockstep with list_tools(): a new
    # tool with no table entry would ship unannotated, a stale entry is dead weight.
    advertised = {t.name for t in _advertised_tools()}
    assert set(server._TOOL_ANNOTATIONS) == advertised


def test_annotation_classifications_are_honest():
    by_name = {t.name: t.annotations for t in _advertised_tools()}

    # Paid model calls: never read-only (they cost money + mutate server state),
    # reach the open world, and are non-idempotent (each call spends again).
    ask = by_name["ask"]
    assert ask.readOnlyHint is False and ask.openWorldHint is True
    assert ask.idempotentHint is False and ask.destructiveHint is False

    # Pure local read.
    stats = by_name["stats"]
    assert stats.readOnlyHint is True and stats.openWorldHint is False

    # Read-only but network-backed catalog.
    assert by_name["list_models"].readOnlyHint is True
    assert by_name["list_models"].openWorldHint is True

    # Data-losing local ops are flagged destructive.
    for name in ("context", "reset_session"):
        assert by_name[name].destructiveHint is True, name
        assert by_name[name].readOnlyHint is False, name

    # Config writes mutate local state but are additive/restorable, not destructive.
    cfg = by_name["configure_tracing"]
    assert cfg.readOnlyHint is False and cfg.destructiveHint is False
