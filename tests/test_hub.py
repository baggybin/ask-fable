"""Cross-instance session hub — visibility-only mirror.

The hub mirrors completed turns into a shared SQLite file so the operator (and
agents, via ``session_list`` / ``session_peek`` / ``session_stats``) can see what
every instance on the machine is asking the oracles. Each test points the hub at
an isolated ``ASK_FABLE_HUB_PATH`` so they don't collide with real state.

The hard contract under test: the hub is write-only-from-success-paths and
read-only-from-the-three-tools; it never feeds back into an oracle's answer. The
integration tests assert that ``_handle_ask`` / ``_handle_single`` / council /
chain / debate mirror turns on the OK path and do NOT mirror on refused/error
paths (refused/error turns aren't useful visibility and aren't mirrored).
"""

from __future__ import annotations

import asyncio
import time

import ask_fable.hub as hub
import ask_fable.server as server
from ask_fable.oracle_common import OracleResult
from ask_fable.sessions import SessionStore


def _run(coro):
    return asyncio.run(coro)


def _enabled_hub(monkeypatch, tmp_path):
    """Point the hub at a throwaway db and ensure it's enabled."""
    p = tmp_path / "hub.db"
    monkeypatch.setenv("ASK_FABLE_HUB_PATH", str(p))
    monkeypatch.delenv("ASK_FABLE_HUB", raising=False)
    # Reset the per-process sweep counter so sweep cadence tests are deterministic.
    hub._sweep_counter = 0
    return p


# ── write_turn + list_sessions + peek_session ───────────────────────────


def test_write_then_list_one_session(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    hub.write_turn(
        agent_id="opencode-1", project="proj-a", session_key="dup-events",
        question="Why does the cache invalidate early?", answer="Because of X.",
        oracle="claude-fable-5", status="ok", duration_ms=4200,
    )
    out = hub.list_sessions(project="proj-a")
    assert out["status"] == "ok"
    assert out["enabled"] is True
    assert len(out["sessions"]) == 1
    s = out["sessions"][0]
    assert s["session_key"] == "dup-events"
    assert s["agent_id"] == "opencode-1"
    assert s["last_oracle"] == "claude-fable-5"
    assert s["last_status"] == "ok"
    assert s["turn_count"] == 1
    assert s["last_heartbeat_age_s"] is not None and s["last_heartbeat_age_s"] >= 0
    assert s["stale"] is False


def test_multiple_turns_increment_turn_count(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    for i in range(3):
        hub.write_turn(
            agent_id="a1", project="p1", session_key="s1",
            question=f"q{i}", answer=f"a{i}", oracle="claude-fable-5", status="ok",
        )
    out = hub.list_sessions(project="p1")
    assert out["sessions"][0]["turn_count"] == 3
    peek = hub.peek_session(session_key="s1")
    assert len(peek["turns"]) == 3
    assert [t["question"] for t in peek["turns"]] == ["q0", "q1", "q2"]


def test_same_session_across_projects_does_not_collide(monkeypatch, tmp_path):
    # ST-2: the same (session_key, agent_id) used in two projects on one machine used to
    # collide (project was left out of the PK), so the second write flipped the row's
    # project and the first project's dashboard went blank. With project in the PK, both
    # projects keep their own row.
    _enabled_hub(monkeypatch, tmp_path)
    for proj in ("p1", "p2"):
        hub.write_turn(
            agent_id="a1", project=proj, session_key="s1",
            question=f"q-{proj}", answer="a", oracle="claude-fable-5", status="ok",
        )
    out1 = hub.list_sessions(project="p1")
    out2 = hub.list_sessions(project="p2")
    assert len(out1["sessions"]) == 1 and out1["sessions"][0]["last_question"] == "q-p1"
    assert len(out2["sessions"]) == 1 and out2["sessions"][0]["last_question"] == "q-p2"


def test_migrates_old_session_meta_pk(monkeypatch, tmp_path):
    # ST-2: a DB carrying the pre-fix PK (session_key, agent_id) must be rebuilt. A bare
    # CREATE TABLE IF NOT EXISTS would silently keep the old PK, and the new
    # ON CONFLICT(project, …) would then raise and be swallowed — stopping the mirror.
    import sqlite3

    p = _enabled_hub(monkeypatch, tmp_path)
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE session_meta (session_key TEXT, agent_id TEXT, project TEXT, "
        "last_question TEXT, last_answer TEXT, last_oracle TEXT, last_status TEXT, "
        "sdk_session_id TEXT, last_heartbeat REAL, started REAL, turn_count INTEGER, "
        "PRIMARY KEY (session_key, agent_id))"
    )
    conn.execute(
        "INSERT INTO session_meta (session_key, agent_id, project, turn_count, last_heartbeat) "
        "VALUES ('s1', 'a1', 'pold', 5, ?)",
        (time.time(),),
    )
    conn.commit()
    conn.close()
    # A write drives _connect -> migration, then the project-scoped upsert (would raise on
    # the old PK). It must not raise, and the mirror must keep working.
    hub.write_turn(agent_id="a1", project="pnew", session_key="s1",
                   question="q", answer="a", oracle="claude-fable-5", status="ok")
    conn = sqlite3.connect(str(p))
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_meta'"
    ).fetchone()[0]
    conn.close()
    assert "PRIMARY KEY (project, session_key, agent_id)" in sql
    out = hub.list_sessions(project="pnew")
    assert len(out["sessions"]) == 1 and out["sessions"][0]["turn_count"] == 1


def test_peek_returns_full_turn_history(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    hub.write_turn(
        agent_id="a1", project="p1", session_key="s1",
        question="full question text here", answer="full answer text here",
        oracle="glm-5.2", status="ok", duration_ms=100,
    )
    peek = hub.peek_session(session_key="s1")
    assert peek["status"] == "ok"
    t = peek["turns"][0]
    assert t["question"] == "full question text here"
    assert t["answer"] == "full answer text here"
    assert t["oracle"] == "glm-5.2"
    assert t["duration_ms"] == 100


def test_list_scoped_to_current_project_by_default(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    hub.write_turn(agent_id="a1", project="mine", session_key="s1", question="q", answer="a", oracle="o", status="ok")
    hub.write_turn(agent_id="a2", project="other", session_key="s2", question="q", answer="a", oracle="o", status="ok")
    mine = hub.list_sessions(project="mine")
    assert [s["session_key"] for s in mine["sessions"]] == ["s1"]
    other = hub.list_sessions(project="other")
    assert [s["session_key"] for s in other["sessions"]] == ["s2"]


def test_list_all_projects_shows_everything(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    hub.write_turn(agent_id="a1", project="p1", session_key="s1", question="q", answer="a", oracle="o", status="ok")
    hub.write_turn(agent_id="a2", project="p2", session_key="s2", question="q", answer="a", oracle="o", status="ok")
    out = hub.list_sessions(project="p1", all_projects=True)
    keys = sorted(s["session_key"] for s in out["sessions"])
    assert keys == ["s1", "s2"]


def test_peek_scoped_to_agent_id(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    hub.write_turn(agent_id="a1", project="p1", session_key="shared", question="q1", answer="a1", oracle="o", status="ok")
    hub.write_turn(agent_id="a2", project="p1", session_key="shared", question="q2", answer="a2", oracle="o", status="ok")
    peek = hub.peek_session(session_key="shared", agent_id="a1")
    assert len(peek["turns"]) == 1
    assert peek["turns"][0]["agent_id"] == "a1"


def test_stale_flag_when_heartbeat_is_old(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    monkeypatch.setenv("ASK_FABLE_HUB_STALE_SECONDS", "1")
    hub.write_turn(agent_id="a1", project="p1", session_key="s1", question="q", answer="a", oracle="o", status="ok")
    # Manually backdate the heartbeat past the staleness threshold.
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "hub.db"))
    conn.execute("UPDATE session_meta SET last_heartbeat = ?", (time.time() - 100,))
    conn.commit()
    conn.close()
    # Default active_only=True hides stale sessions from the live dashboard.
    live = hub.list_sessions(project="p1")
    assert live["active_only"] is True
    assert live["sessions"] == []
    # Archaeology: include history and surface the stale flag.
    out = hub.list_sessions(project="p1", active_only=False)
    assert len(out["sessions"]) == 1
    assert out["sessions"][0]["stale"] is True


def test_active_only_false_includes_stale_and_fresh(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    monkeypatch.setenv("ASK_FABLE_HUB_STALE_SECONDS", "1")
    hub.write_turn(agent_id="fresh", project="p1", session_key="live", question="q", answer="a", oracle="o", status="ok")
    hub.write_turn(agent_id="old", project="p1", session_key="dead", question="q", answer="a", oracle="o", status="ok")
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "hub.db"))
    conn.execute(
        "UPDATE session_meta SET last_heartbeat = ? WHERE session_key = ?",
        (time.time() - 100, "dead"),
    )
    conn.commit()
    conn.close()
    live = hub.list_sessions(project="p1", active_only=True)
    assert [s["session_key"] for s in live["sessions"]] == ["live"]
    all_sess = hub.list_sessions(project="p1", active_only=False)
    keys = {s["session_key"] for s in all_sess["sessions"]}
    assert keys == {"live", "dead"}


# ── hub_stats ───────────────────────────────────────────────────────────


def test_hub_stats_aggregates_across_agents(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    hub.write_turn(agent_id="a1", project="p1", session_key="s1", question="q", answer="a", oracle="claude-fable-5", status="ok")
    hub.write_turn(agent_id="a2", project="p1", session_key="s2", question="q", answer="a", oracle="glm-5.2", status="ok")
    hub.write_turn(agent_id="a1", project="p1", session_key="s1", question="q2", answer="a2", oracle="claude-fable-5", status="ok")
    out = hub.hub_stats(project="p1")
    assert out["status"] == "ok"
    assert out["total_turns"] == 3
    assert out["window_s"] == hub._DEFAULT_STATS_WINDOW_S
    assert out["total_sessions"] == 2
    assert out["fresh_sessions"] == 2
    assert out["active_sessions"] == 2  # alias of fresh_sessions
    assert out["attributed_turns"] == 3
    assert out["unknown_turns"] == 0
    assert out["by_status"]["ok"] == 3
    assert out["by_oracle"]["claude-fable-5"] == 2
    assert out["by_oracle"]["glm-5.2"] == 1
    assert out["by_agent"]["a1"] == 2
    assert out["by_agent"]["a2"] == 1


def test_hub_stats_scoped_to_project(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    hub.write_turn(agent_id="a1", project="p1", session_key="s1", question="q", answer="a", oracle="o", status="ok")
    hub.write_turn(agent_id="a1", project="p2", session_key="s2", question="q", answer="a", oracle="o", status="ok")
    scoped = hub.hub_stats(project="p1")
    assert scoped["total_turns"] == 1
    allp = hub.hub_stats(project="p1", all_projects=True)
    assert allp["total_turns"] == 2


def test_hub_stats_window_excludes_old_turns(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    hub.write_turn(agent_id="a1", project="p1", session_key="s1", question="new", answer="a", oracle="o", status="ok")
    hub.write_turn(agent_id="unknown", project="p1", session_key="s2", question="old", answer="a", oracle="o", status="ok")
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "hub.db"))
    # Backdate the second turn far outside a short window.
    conn.execute(
        "UPDATE turns SET ts = ? WHERE question = ?",
        (time.time() - 10_000, "old"),
    )
    conn.commit()
    conn.close()
    recent = hub.hub_stats(project="p1", window_s=60)
    assert recent["window_s"] == 60
    assert recent["total_turns"] == 1
    assert recent["by_agent"] == {"a1": 1}
    assert recent["attributed_turns"] == 1
    assert recent["unknown_turns"] == 0
    # window_s=0 → all retained history
    all_time = hub.hub_stats(project="p1", window_s=0)
    assert all_time["window_s"] == 0
    assert all_time["total_turns"] == 2
    assert all_time["unknown_turns"] == 1
    assert all_time["attributed_turns"] == 1


def test_hub_stats_fresh_vs_total_sessions(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    monkeypatch.setenv("ASK_FABLE_HUB_STALE_SECONDS", "1")
    hub.write_turn(agent_id="a1", project="p1", session_key="live", question="q", answer="a", oracle="o", status="ok")
    hub.write_turn(agent_id="a2", project="p1", session_key="dead", question="q", answer="a", oracle="o", status="ok")
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "hub.db"))
    conn.execute(
        "UPDATE session_meta SET last_heartbeat = ? WHERE session_key = ?",
        (time.time() - 100, "dead"),
    )
    conn.commit()
    conn.close()
    out = hub.hub_stats(project="p1", window_s=0)
    assert out["total_sessions"] == 2
    assert out["fresh_sessions"] == 1
    assert out["active_sessions"] == 1


# ── disabled + best-effort ───────────────────────────────────────────────


def test_disabled_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_HUB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("ASK_FABLE_HUB", "0")
    hub.write_turn(agent_id="a1", project="p1", session_key="s1", question="q", answer="a", oracle="o", status="ok")
    out = hub.list_sessions(project="p1")
    assert out["status"] == "ok"
    assert out["enabled"] is False
    assert out["sessions"] == []


def test_write_failure_never_raises(monkeypatch, tmp_path):
    # Point at an unwritable path — every op must degrade to no-op, never raise.
    monkeypatch.setenv("ASK_FABLE_HUB_PATH", "/proc/cannot/exist/hub.db")
    monkeypatch.delenv("ASK_FABLE_HUB", raising=False)
    hub.write_turn(agent_id="a1", project="p1", session_key="s1", question="q", answer="a", oracle="o", status="ok")
    out = hub.list_sessions(project="p1")
    assert out["status"] == "error"  # store unavailable, surfaced cleanly
    assert out["kind"] == "store_unavailable"


# ── retention sweep ─────────────────────────────────────────────────────


def test_sweep_evicts_oldest_when_over_cap(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    monkeypatch.setenv("ASK_FABLE_HUB_MAX_ROWS", "20")  # tiny cap
    # Write enough to trip the every-100-writes sweep would need 100 writes;
    # instead force a sweep directly by lowering the counter trigger and writing 101.
    for i in range(101):
        hub.write_turn(agent_id="a1", project="p1", session_key="s1",
                       question=f"q{i}", answer=f"a{i}", oracle="o", status="ok")
    # After sweep, turns trimmed toward 90% of cap (18).
    peek = hub.peek_session(session_key="s1")
    assert len(peek["turns"]) <= 20


def test_prune_junk_removes_zero_duration_unknown(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    hub.write_turn(
        agent_id="unknown", project="p1", session_key="junk",
        question="synthetic", answer="x", oracle="o", status="ok", duration_ms=0,
    )
    hub.write_turn(
        agent_id="unknown", project="p1", session_key="junk",
        question="also synthetic", answer="y", oracle="o", status="ok", duration_ms=1,
    )
    hub.write_turn(
        agent_id="claude-code", project="p1", session_key="real",
        question="live", answer="z", oracle="o", status="ok", duration_ms=4200,
    )
    # Real-duration unknown (rare) is kept — only the zero/1ms synthetic pattern.
    hub.write_turn(
        agent_id="unknown", project="p1", session_key="real-unknown",
        question="maybe real", answer="w", oracle="o", status="ok", duration_ms=1500,
    )
    out = hub.prune_junk()
    assert out["status"] == "ok"
    assert out["deleted_turns"] == 2
    assert out["remaining_turns"] == 2
    keys = {s["session_key"] for s in hub.list_sessions(project="p1", active_only=False)["sessions"]}
    assert keys == {"real", "real-unknown"}
    assert hub.peek_session(session_key="junk")["turns"] == []


def _two_projects_one_label(p, *, a_turns, b_turns):
    """Projects pA then pB, each on the default session with the same agent id — two
    session_meta rows. ts is pinned to insertion order so "oldest"/"latest" are exact."""
    import sqlite3

    for proj, n in (("pA", a_turns), ("pB", b_turns)):
        for i in range(n):
            hub.write_turn(agent_id="claude-code", project=proj, session_key="default",
                           question=f"{proj} q{i}", answer=f"{proj} a{i}", oracle="o",
                           status="ok", duration_ms=5000)
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE turns SET ts = id")
    conn.commit()
    conn.close()


def _meta_by_project(p):
    import sqlite3

    conn = sqlite3.connect(str(p))
    try:
        return {
            proj: (count, q)
            for proj, count, q in conn.execute(
                "SELECT project, turn_count, last_question FROM session_meta"
            )
        }
    finally:
        conn.close()


def test_prune_junk_keeps_projects_apart(monkeypatch, tmp_path):
    """The recount matched only (session_key, agent_id): both projects got the COMBINED
    turn_count, and pA's row was rewritten with pB's last question and answer."""
    p = _enabled_hub(monkeypatch, tmp_path)
    _two_projects_one_label(p, a_turns=2, b_turns=5)
    assert hub.prune_junk()["status"] == "ok"
    assert _meta_by_project(p) == {"pA": (2, "pA q1"), "pB": (5, "pB q4")}


# ── integration: _handle_ask mirrors on OK, not on refused/error ─────────


def _stub_fable_ok(monkeypatch, answer="answer text"):
    async def fake_run(question, context="", *, resume=None, on_think=None):
        fake_run.calls.append({"question": question, "resume": resume})
        return OracleResult("ok", text=answer, session_id="sid-1")
    fake_run.calls = []
    monkeypatch.setattr(server.fable, "run", fake_run)
    return fake_run


def test_handle_ask_mirrors_ok_turn_to_hub(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    monkeypatch.setattr(server.guard, "check_denylist", lambda p: (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    _stub_fable_ok(monkeypatch, "the real answer")
    monkeypatch.setenv("ASK_FABLE_AGENT_ID", "test-agent")
    # _handle_ask is called directly (not via call_tool), so simulate call_tool's
    # per-call ContextVar set that _hub_mirror reads for attribution.
    token = server._CALL_AGENT_ID.set("test-agent")
    try:
        out = _run(server._handle_ask(SessionStore(), {"question": "How does routing work?", "session": "routing"}))
    finally:
        server._CALL_AGENT_ID.reset(token)
    assert out["status"] == "ok"
    listing = hub.list_sessions(project="any", all_projects=True)
    assert len(listing["sessions"]) == 1
    s = listing["sessions"][0]
    assert s["agent_id"] == "test-agent"
    assert s["session_key"] == "routing"
    assert s["last_status"] == "ok"
    peek = hub.peek_session(session_key="routing")
    assert peek["turns"][0]["answer"] == "the real answer"


def test_handle_ask_does_not_mirror_on_refused(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    monkeypatch.setattr(server.guard, "check_denylist", lambda p: (False, "offensive-security content"))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    _stub_fable_ok(monkeypatch, "should not be mirrored")
    _run(server._handle_ask(SessionStore(), {"question": "blocked question"}))
    listing = hub.list_sessions(project="any", all_projects=True)
    assert listing["sessions"] == []  # refused at guard → nothing mirrored


def test_handle_ask_does_not_mirror_on_model_refused(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    monkeypatch.setattr(server.guard, "check_denylist", lambda p: (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    async def fake_run(question, context="", *, resume=None, on_think=None):
        return OracleResult("refused", text="off-scope")
    monkeypatch.setattr(server.fable, "run", fake_run)
    _run(server._handle_ask(SessionStore(), {"question": "too broad"}))
    listing = hub.list_sessions(project="any", all_projects=True)
    assert listing["sessions"] == []


# ── multi-oracle tools also mirror on OK ────────────────────────────────


def _allow_guard(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="", *, trusted=False: (True, ""))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE", "0")  # force live path, not cache hit


def test_council_mirrors_ok_to_hub(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    _allow_guard(monkeypatch)
    token = server._CALL_AGENT_ID.set("council-agent")

    async def fake_fable(question, context="", *, resume=None, system_prompt=None, on_think=None):
        if system_prompt is not None:
            return OracleResult("ok", text="MERGED council answer", model="claude-fable-5")
        return OracleResult("ok", text="fable panelist", model="claude-fable-5")

    async def fake_minimax(question, context="", **kw):
        return OracleResult("ok", text="minimax panelist", model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)
    try:
        out = _run(server._handle_council({
            "question": "Is the routing design sound?",
            "models": ["fable", "minimax"],
        }))
    finally:
        server._CALL_AGENT_ID.reset(token)
    assert out["status"] == "ok"
    listing = hub.list_sessions(project="any", all_projects=True)
    assert len(listing["sessions"]) == 1
    s = listing["sessions"][0]
    # No session arg → fall back to tool title so multi-oracle stays visible.
    assert s["session_key"] == "ask_council"
    assert s["agent_id"] == "council-agent"
    assert s["last_status"] == "ok"
    peek = hub.peek_session(session_key="ask_council")
    assert peek["turns"][0]["answer"] == "MERGED council answer"
    assert peek["turns"][0]["oracle"] == server.fable.fable_model()


def test_council_hub_uses_caller_session(monkeypatch, tmp_path):
    """Caller session groups multi-oracle turns with single-oracle work on the hub."""
    _enabled_hub(monkeypatch, tmp_path)
    _allow_guard(monkeypatch)
    token = server._CALL_AGENT_ID.set("council-agent")

    async def fake_fable(question, context="", *, resume=None, system_prompt=None, on_think=None):
        if system_prompt is not None:
            return OracleResult("ok", text="MERGED", model="claude-fable-5")
        return OracleResult("ok", text="fable", model="claude-fable-5")

    async def fake_minimax(question, context="", **kw):
        return OracleResult("ok", text="mmx", model="MiniMax-M3")

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)
    try:
        out = _run(server._handle_council({
            "question": "Is the routing design sound?",
            "models": ["fable", "minimax"],
            "session": "quiescence-design",
        }))
    finally:
        server._CALL_AGENT_ID.reset(token)
    assert out["status"] == "ok"
    listing = hub.list_sessions(project="any", all_projects=True)
    assert [s["session_key"] for s in listing["sessions"]] == ["quiescence-design"]
    peek = hub.peek_session(session_key="quiescence-design")
    assert peek["turns"][0]["answer"] == "MERGED"


def test_council_does_not_mirror_on_guard_refuse(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    monkeypatch.setattr(server.guard, "check", lambda q, c="", *, trusted=False: (False, "blocked"))
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE", "0")
    _run(server._handle_council({"question": "blocked question here"}))
    listing = hub.list_sessions(project="any", all_projects=True)
    assert listing["sessions"] == []


def test_chain_mirrors_ok_to_hub(monkeypatch, tmp_path):
    _enabled_hub(monkeypatch, tmp_path)
    _allow_guard(monkeypatch)
    token = server._CALL_AGENT_ID.set("chain-agent")

    async def fake_run(key, question, context="", *, effort=None):
        return OracleResult("ok", key=key, text=f"stage-{key} answer", model=key)

    monkeypatch.setattr(server.oracles, "run", fake_run)
    try:
        out = _run(server._handle_chain({
            "question": "Should we split the module?",
            "models": ["minimax", "fable"],
        }))
    finally:
        server._CALL_AGENT_ID.reset(token)
    assert out["status"] == "ok"
    listing = hub.list_sessions(project="any", all_projects=True)
    assert len(listing["sessions"]) == 1
    s = listing["sessions"][0]
    assert s["session_key"] == "ask_chain"
    assert s["agent_id"] == "chain-agent"
    peek = hub.peek_session(session_key="ask_chain")
    # final stage is fable
    assert peek["turns"][0]["oracle"] == "fable"
    assert "stage-fable" in peek["turns"][0]["answer"]


def test_debate_mirrors_ok_to_hub(monkeypatch, tmp_path):
    """Debate that degrades to single-critic still returns status ok — must mirror."""
    _enabled_hub(monkeypatch, tmp_path)
    _allow_guard(monkeypatch)
    token = server._CALL_AGENT_ID.set("debate-agent")

    call_n = {"n": 0}

    async def fake_run(key, question, context="", *, effort=None):
        call_n["n"] += 1
        if key == "fable":
            # propose (and any later fable turns)
            body = (
                "I propose approach A.\n\n"
                "```json\n"
                '{"recommendation": "apply", "confidence": "high", "needs_context": []}\n'
                "```"
            )
            return OracleResult("ok", key=key, text=body, model="claude-fable-5")
        # opponent fails → degraded_single_critic finish path
        return OracleResult("error", key=key, kind="not_configured", text="no mmx", model="minimax")

    monkeypatch.setattr(server.oracles, "run", fake_run)
    try:
        out = _run(server._handle_debate({
            "question": "Is approach A better than B for this API?",
            "proposer": "fable",
            "opponent": "minimax",
            "rounds": 1,
        }))
    finally:
        server._CALL_AGENT_ID.reset(token)
    assert out["status"] == "ok"
    listing = hub.list_sessions(project="any", all_projects=True)
    assert len(listing["sessions"]) == 1
    s = listing["sessions"][0]
    assert s["session_key"] == "ask_debate"
    assert s["agent_id"] == "debate-agent"
    peek = hub.peek_session(session_key="ask_debate")
    assert peek["turns"][0]["oracle"] == "claude-fable-5"
    assert "approach A" in peek["turns"][0]["answer"]


# ── schema builders ─────────────────────────────────────────────────────


def test_q_ctx_schemas_share_context_ref_shape():
    """Single-oracle tools must share one context_ref anyOf shape (no drift)."""
    shared = server._CONTEXT_REF_PROP
    for schema in (
        server._M3_SCHEMA,
        server._GLM_SCHEMA,
        server._DEEPSEEK_SCHEMA,
        server._GEMINI_SCHEMA,
        server._CODEX_SCHEMA,
        server._OLLAMA_SCHEMA,
        server._ATLAS_SCHEMA,
        server._COUNCIL_SCHEMA,
        server._CHAIN_SCHEMA,
        server._DEBATE_SCHEMA,
        server._OLLAMA_COUNCIL_SCHEMA,
    ):
        cref = schema["properties"]["context_ref"]
        assert cref["anyOf"] == shared["anyOf"]
        assert 'context(op="write"' in cref["description"]
    # Gemini label matches the live default model family
    assert "Gemini 3.1 Pro" in server._GEMINI_SCHEMA["properties"]["question"]["description"]


def test_multi_oracle_schemas_expose_hub_session():
    """Council/chain/debate share the hub coordination session property."""
    for schema in (
        server._COUNCIL_SCHEMA,
        server._CHAIN_SCHEMA,
        server._DEBATE_SCHEMA,
        server._OLLAMA_COUNCIL_SCHEMA,
    ):
        sess = schema["properties"]["session"]
        assert sess == server._HUB_SESSION_PROP
        assert "session_list" in sess["description"]


def test_sweep_recomputes_turn_count_after_eviction(monkeypatch, tmp_path):
    """S6: turn_count is bumped per write; after a sweep evicts turns it must be
    recomputed from surviving rows, not left inflated (session_stats over-reported)."""
    import sqlite3
    p = _enabled_hub(monkeypatch, tmp_path)
    monkeypatch.setenv("ASK_FABLE_HUB_MAX_ROWS", "5")
    for i in range(12):
        hub.write_turn(agent_id="a", project="p", session_key="s",
                       question=f"q{i}", answer="x", oracle="o", status="ok")
    conn = sqlite3.connect(str(p))
    try:
        hub._sweep(conn)
        meta = conn.execute("SELECT turn_count FROM session_meta WHERE session_key='s'").fetchone()[0]
        surviving = conn.execute("SELECT COUNT(*) FROM turns WHERE session_key='s'").fetchone()[0]
    finally:
        conn.close()
    assert meta == surviving and meta < 12


def test_sweep_keeps_projects_apart(monkeypatch, tmp_path):
    """The sweep's orphan delete and recount matched only (session_key, agent_id): once
    pA's turns were all evicted its row survived on pB's turns (a phantom session on
    pA's dashboard), and both rows took the combined count."""
    import sqlite3

    p = _enabled_hub(monkeypatch, tmp_path)
    monkeypatch.setenv("ASK_FABLE_HUB_MAX_ROWS", "10")
    _two_projects_one_label(p, a_turns=3, b_turns=12)
    conn = sqlite3.connect(str(p))
    try:
        hub._sweep(conn)  # 15 > 10: evicts the 6 oldest — all of pA's, 3 of pB's
    finally:
        conn.close()
    assert _meta_by_project(p) == {"pB": (9, "pB q11")}
    assert hub.list_sessions(project="pA", active_only=False)["sessions"] == []
