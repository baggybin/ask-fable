"""ask_fable conference handler — sequential rounds, rapporteur, degrade paths.

Drives ``_handle_conference`` with ``oracles.run`` stubbed and the guard allowed,
ASK_FABLE_QUIET=1 so the console reporter stays silent. No model is called. Every
bench model counts as available (no keys/CLIs on a test host) unless a test says
otherwise.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import ask_fable.server as server
from ask_fable import conference, oracles
from ask_fable.oracle_common import OracleResult


@pytest.fixture(autouse=True)
def _quiet_and_no_audit(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)
    monkeypatch.setattr(oracles, "available", lambda key: True)


def _run(coro):
    return asyncio.run(coro)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="", **kw: (True, ""))


def _stub_run(monkeypatch, mapping=None):
    """Stub oracles.run: per-key canned turns; the rapporteur call returns a map."""
    calls: list[tuple[str, str]] = []

    async def fake_run(key, question, context="", **kw):
        calls.append((key, question))
        if "MAP OF THE DISAGREEMENT" in question:  # the synthesis call
            return OracleResult("ok", key=key, text="CONVERGED: caching. CRUX: none.", model=key)
        text = (mapping or {}).get(key, f"{key} makes a point")
        return OracleResult("ok", key=key, text=text, model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    return calls


def test_runs_rounds_and_produces_map(monkeypatch):
    _allow(monkeypatch)
    calls = _stub_run(monkeypatch)
    out = _run(
        server._handle_conference(
            {"question": "cache in redis or postgres?", "models": ["fable", "minimax"], "rounds": 2}
        )
    )
    assert out["status"] == "ok"
    assert out["rounds"] == 2
    assert len(out["posts"]) == 4  # 2 models × 2 rounds
    assert out["map"] == "CONVERGED: caching. CRUX: none."
    # 4 turn calls + 1 synthesis
    assert len(calls) == 5
    # each debater's contribution is on the transcript
    joined = "\n".join(out["transcript"])
    assert "TOPIC: cache in redis or postgres?" in joined


def test_round_one_is_blind(monkeypatch):
    _allow(monkeypatch)
    calls = _stub_run(monkeypatch)
    _run(
        server._handle_conference(
            {"question": "cache or shard?", "models": ["fable", "minimax"], "rounds": 2}
        )
    )
    turns = [q for (_k, q) in calls if "MAP OF THE DISAGREEMENT" not in q]
    # round 1 (first two turns) is blind — no peer contribution visible yet
    assert "makes a point" not in turns[0]
    assert "makes a point" not in turns[1]
    assert "cache or shard?" in turns[0]  # but the topic is
    # round 2 reveals the committed round-1 positions
    assert "makes a point" in turns[2]


def test_guard_denied_calls_no_model(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="", **kw: (False, "prohibited_x"))
    calls = _stub_run(monkeypatch)
    out = _run(server._handle_conference({"question": "blocked topic here", "models": ["fable", "minimax"]}))
    assert out["status"] == "refused"
    assert out["reason"] == "prohibited_x"
    assert calls == []  # nothing dispatched


def test_needs_at_least_two_models(monkeypatch):
    _allow(monkeypatch)
    _stub_run(monkeypatch)
    out = _run(server._handle_conference({"question": "x", "models": ["fable"]}))
    assert out["status"] == "error"
    assert out["kind"] == "no_models"


def test_missing_topic_is_bad_args(monkeypatch):
    _allow(monkeypatch)
    _stub_run(monkeypatch)
    out = _run(server._handle_conference({"models": ["fable", "minimax"]}))
    assert out["status"] == "error"
    assert out["kind"] == "bad_args"


def test_rounds_are_clamped(monkeypatch):
    _allow(monkeypatch)
    _stub_run(monkeypatch)
    out = _run(
        server._handle_conference(
            {"question": "x", "models": ["fable", "minimax"], "rounds": 99}
        )
    )
    assert out["rounds"] == 10  # clamped to the max


def test_unavailable_models_are_flagged_up_front_not_seated(monkeypatch):
    # An explicit bench skipped the availability check the default bench gets: an
    # unconfigured model took a seat, failed every turn, and was still listed.
    _allow(monkeypatch)
    monkeypatch.setattr(oracles, "available", lambda key: key != "glm")
    calls = _stub_run(monkeypatch)
    out = _run(
        server._handle_conference(
            {"question": "redis or postgres?", "models": ["fable", "minimax", "glm"], "rounds": 1}
        )
    )
    assert out["status"] == "ok"
    assert out["unavailable"] == ["glm"]
    assert out["models"] == [oracles.label("fable"), oracles.label("minimax")]
    assert all(key != "glm" for key, _ in calls)

    # with one available model left there is no conference to run
    calls.clear()
    out = _run(server._handle_conference({"question": "x?", "models": ["fable", "glm"]}))
    assert out["status"] == "error" and out["kind"] == "no_models"
    assert out["unavailable"] == ["glm"] and calls == []


def test_failed_turns_and_a_failed_map_are_reported(monkeypatch):
    # Failures used to vanish: status ok, the silent model still listed, and map ""
    # with no reason — indistinguishable from a real two-voice conference.
    _allow(monkeypatch)

    async def fake_run(key, question, context="", **kw):
        if "MAP OF THE DISAGREEMENT" in question:
            return OracleResult("error", key=key, kind="timeout", text="map timed out", model=key)
        if key == "minimax":
            return OracleResult("error", key=key, kind="auth", text="not logged in", model=key)
        return OracleResult("ok", key=key, text=f"{key} makes a point", model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    out = _run(
        server._handle_conference(
            {"question": "redis or postgres?", "models": ["fable", "minimax"], "rounds": 2}
        )
    )
    assert out["status"] == "ok"  # only fable spoke: reported via degraded/detail
    assert out["detail"] == "only 1 of 2 participants spoke"
    assert out["quorum"] == "1/2" and out["degraded"] is True
    m3 = oracles.label("minimax")
    assert [(e["model"], e["round"], e["kind"]) for e in out["errors"]] == [
        (m3, 1, "auth"),
        (m3, 2, "auth"),
    ]
    assert out["map"] == ""
    assert out["map_error"]["kind"] == "timeout" and out["map_error"]["detail"] == "map timed out"


def test_session_reaches_the_hub_and_the_call_is_audited_and_saved(monkeypatch):
    # `session` was declared in the schema but never read, and nothing reached the
    # hub, the audit log or the answer store — unlike every other ask_* handler.
    from ask_fable import hub

    _allow(monkeypatch)
    _stub_run(monkeypatch)
    audits, saves = [], []
    monkeypatch.setattr(server.audit, "record", lambda **k: audits.append(k))
    monkeypatch.setattr(server.outputs, "save", lambda **k: saves.append(k) or "/tmp/c.md")
    out = _run(
        server._handle_conference(
            {
                "question": "redis or postgres?",
                "models": ["fable", "minimax"],
                "rounds": 1,
                "session": "design-review-42",
            }
        )
    )
    assert out["status"] == "ok" and out["saved"] == "/tmp/c.md"
    assert out["quorum"] == "2/2" and out["degraded"] is False
    turns = hub.peek_session(session_key="design-review-42")["turns"]
    assert [t["answer"] for t in turns] == ["CONVERGED: caching. CRUX: none."]
    assert audits[-1]["decision"] == "allowed" and audits[-1]["quorum"] == "2/2"
    assert saves[0]["tool"] == "ask_conference" and saves[0]["answer"] == out["map"]


def test_tool_is_registered_with_schema():
    # description + schema wired without instantiating the server closure
    assert isinstance(server.ASK_CONFERENCE_TOOL_DESCRIPTION, str)
    props = server._CONFERENCE_SCHEMA["properties"]
    assert {"models", "rounds", "synthesizer", "interactive"} <= set(props)


def test_default_candidates_are_reasonable():
    assert {"fable", "deepseek", "minimax"} <= set(conference.CANDIDATES)


def _fake_server(session):
    return SimpleNamespace(request_context=SimpleNamespace(session=session))


def test_elicitation_picker_returns_selection():
    class _Session:
        client_params = SimpleNamespace(
            capabilities=SimpleNamespace(elicitation=SimpleNamespace(form=SimpleNamespace()))
        )

        async def elicit_form(self, message, schema):
            self.schema = schema
            content = {"topic": "cache or shard?", "fable": True, "minimax": True, "rounds": 4}
            return SimpleNamespace(action="accept", content=content)

    session = _Session()
    out = _run(server._elicit_conference_setup(_fake_server(session), {}))
    assert out["action"] == "accept"
    assert out["topic"] == "cache or shard?"
    assert set(out["models"]) == {"fable", "minimax"}
    assert out["rounds"] == 4
    # the picker schema offers each candidate as a checkbox + a topic + rounds
    props = session.schema["properties"]
    assert "topic" in props and "rounds" in props
    assert all(brain in props for brain in conference.CANDIDATES)


def test_elicitation_falls_back_when_unsupported():
    session = SimpleNamespace(
        client_params=SimpleNamespace(capabilities=SimpleNamespace(elicitation=None))
    )
    out = _run(server._elicit_conference_setup(_fake_server(session), {}))
    assert out["action"] == "fallback"


def test_elicitation_declined_is_passed_through():
    class _Session:
        client_params = SimpleNamespace(
            capabilities=SimpleNamespace(elicitation=SimpleNamespace(form=SimpleNamespace()))
        )

        async def elicit_form(self, message, schema):
            return SimpleNamespace(action="decline", content=None)

    out = _run(server._elicit_conference_setup(_fake_server(_Session()), {}))
    assert out["action"] == "decline"


def test_l3_unknown_seats_count_in_the_quorum_denominator(monkeypatch):
    """L3 (bug hunt 2026-09-25): a seat nothing recognized was ASKED FOR and did not
    speak, so it belongs in the denominator the way the council counts it. Leaving it
    out reported a full "2/2" for a list the council called "2/3, degraded"."""
    _allow(monkeypatch)
    _stub_run(monkeypatch)
    out = _run(
        server._handle_conference(
            {
                "question": "cache in redis or postgres?",
                "models": ["fable", "minimax", "gpt-5"],
                "rounds": 1,
            }
        )
    )
    assert out["status"] == "ok"
    assert out["unknown"] == ["gpt-5"]
    assert out["quorum"] == "2/3"
    assert out["degraded"] is True


def test_c2_the_blind_round_runs_concurrently(monkeypatch):
    """Round 1 is blind BY CONSTRUCTION — no participant can see another — so
    serializing it bought nothing but wall-clock. A 5-model, 3-round run was 16
    calls in series."""
    import asyncio

    _allow(monkeypatch)
    in_flight = 0
    peak = 0

    async def fake_run(key, question, context="", **kw):
        nonlocal in_flight, peak
        if "MAP OF THE DISAGREEMENT" in question:
            return OracleResult("ok", key=key, text="CONVERGED: x", model=key)
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)  # yield, so overlap is observable
        in_flight -= 1
        return OracleResult("ok", key=key, text=f"{key} opens", model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    out = _run(
        server._handle_conference(
            {"question": "redis or postgres?", "models": ["fable", "minimax", "glm"], "rounds": 1}
        )
    )
    assert out["status"] == "ok"
    assert peak == 3, f"blind round still serialized (peak in-flight {peak})"


def test_c2_commit_order_follows_the_bench_not_the_clock(monkeypatch):
    """The transcript is the record of the conference, so which seat is committed
    first must be a property of the bench, not of whichever backend was fastest."""
    import asyncio

    _allow(monkeypatch)

    async def fake_run(key, question, context="", **kw):
        if "MAP OF THE DISAGREEMENT" in question:
            return OracleResult("ok", key=key, text="CONVERGED: x", model=key)
        if key == "fable":  # the first bench seat is the slowest
            await asyncio.sleep(0.05)
        return OracleResult("ok", key=key, text=f"{key} opens", model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    out = _run(
        server._handle_conference(
            {"question": "redis or postgres?", "models": ["fable", "minimax", "glm"], "rounds": 1}
        )
    )
    bench = ["fable", "minimax", "glm"]
    assert [p["model"] for p in out["posts"]] == [oracles.label(k) for k in bench]


def test_c3_the_rapporteur_sees_no_real_model_names(monkeypatch):
    """The default synthesizer sits on the default bench, so it was writing the map
    of an argument it had taken part in, with everyone named. The council solved
    exactly this for its synthesizer; the conference never inherited it."""
    _allow(monkeypatch)
    seen: dict[str, str] = {}

    async def fake_run(key, question, context="", **kw):
        if "MAP OF THE DISAGREEMENT" in question:
            seen["map"] = question
            return OracleResult("ok", key=key, text="CONVERGED: x. THE CRUX: y.", model=key)
        return OracleResult("ok", key=key, text=f"{key} opens", model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    bench = ["fable", "minimax", "glm"]
    out = _run(
        server._handle_conference(
            {"question": "redis or postgres?", "models": bench, "rounds": 1}
        )
    )
    prompt = seen["map"]
    for key in bench:
        assert f"[{oracles.label(key)} · " not in prompt, f"{key} named to the rapporteur"
    assert "[Participant 1 · " in prompt
    # the rapporteur is `fable` by default and IS on the bench, so it reads its own
    # voice last — the highest-numbered participant.
    assert out["map_legend"][f"Participant {len(bench)}"] == oracles.label("fable")
    # the caller's own view keeps the real names
    assert out["posts"][0]["model"] == oracles.label("fable")
    assert oracles.label("fable") in out["transcript"][1]


def test_c3_the_legend_covers_every_speaker(monkeypatch):
    _allow(monkeypatch)
    _stub_run(monkeypatch)
    bench = ["fable", "minimax", "glm"]
    out = _run(
        server._handle_conference(
            {"question": "redis or postgres?", "models": bench, "rounds": 1}
        )
    )
    assert sorted(out["map_legend"].values()) == sorted(oracles.label(k) for k in bench)
    assert set(out["map_legend"]) == {f"Participant {i + 1}" for i in range(len(bench))}


def test_c4_speaking_order_rotates_each_open_round(monkeypatch):
    """The order was fixed, so the first seat never once argued with peer context and
    the last always had the most — a standing advantage unrelated to what any of
    them thought."""
    _allow(monkeypatch)
    _stub_run(monkeypatch)
    bench = ["fable", "minimax", "glm"]
    out = _run(
        server._handle_conference(
            {"question": "redis or postgres?", "models": bench, "rounds": 3}
        )
    )
    labels = [oracles.label(k) for k in bench]
    by_round: dict[int, list[str]] = {}
    for post in out["posts"]:
        by_round.setdefault(post["round"], []).append(post["model"])
    assert by_round[1] == labels  # round 1 is concurrent, committed in bench order
    assert by_round[2] == labels[1:] + labels[:1]
    assert by_round[3] == labels[2:] + labels[:2]


def test_c4_an_oversized_transcript_keeps_the_blind_round_whole():
    """The blind round is the only independent evidence in the run — the thing
    commit-then-reveal exists to produce — so dropping a seat from it would
    silently change what "blind" means."""
    seed = ["TOPIC: t"]
    blind = [f"[M{i} · round 1] " + "b" * 500 for i in range(3)]
    opens = [f"[M{i % 3} · round {i // 3 + 2}] " + "o" * 500 for i in range(40)]
    out = conference._render(seed + blind + opens, len(seed), len(blind), budget=6_000)
    for line in seed + blind:
        assert line in out
    assert "earlier turns elided" in out
    assert out.endswith(opens[-1])  # the newest turn survives — you argue with it
    assert len(out) < len("\n\n".join(seed + blind + opens))


def test_c4_a_small_transcript_is_untouched():
    seed, blind = ["TOPIC: t"], ["[A · round 1] hi"]
    opens = ["[A · round 2] there"]
    out = conference._render(seed + blind + opens, len(seed), len(blind))
    assert out == "\n\n".join(seed + blind + opens)
    assert "elided" not in out


def test_c5_only_the_last_round_asks_for_a_landed_position(monkeypatch):
    _allow(monkeypatch)
    prompts: list[tuple[int, str]] = []
    seq = {"n": 0}

    async def fake_run(key, question, context="", **kw):
        if "MAP OF THE DISAGREEMENT" in question:
            return OracleResult("ok", key=key, text="CONVERGED: x", model=key)
        seq["n"] += 1
        prompts.append((seq["n"], question))
        return OracleResult("ok", key=key, text=f"{key} says", model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    _run(
        server._handle_conference(
            {"question": "redis or postgres?", "models": ["fable", "minimax"], "rounds": 3}
        )
    )
    closing = [q for _, q in prompts if "Your closing position:" in q]
    assert len(closing) == 2, "exactly the final round lands positions"
    assert all("change your mind" in q for q in closing)
    # rounds 1 and 2 keep the ordinary contribution contract
    assert sum("Your contribution:" in q for _, q in prompts) == 2


# --- Phase 2: attack the shared premise (the conference design) -------


def _stub_premise(monkeypatch, claim_reply: str):
    """Bench answers normally; the premise turn returns `claim_reply`."""
    seen: dict[str, str] = {}

    async def fake_run(key, question, context="", **kw):
        if "MAP OF THE DISAGREEMENT" in question:
            return OracleResult("ok", key=key, text="CONVERGED: x", model=key)
        if "shared premise" in question and "EXACTLY two lines" in question:
            seen["premise_prompt"] = question
            return OracleResult("ok", key=key, text=claim_reply, model=key)
        if "case that this premise is FALSE" in question:
            seen["attack_prompt"] = question
            return OracleResult("ok", key=key, text="the premise fails because …", model=key)
        return OracleResult("ok", key=key, text=f"{key} opens", model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    return seen


def test_c6_the_shared_premise_is_named_and_attacked(monkeypatch):
    """What every opening ASSUMES is the part nobody will challenge, because
    challenging it is not what any of them was asked to do."""
    _allow(monkeypatch)
    seen = _stub_premise(
        monkeypatch, "CLAIM: the write path is the bottleneck\nASSERTED_BY: Participant 1"
    )
    bench = ["fable", "minimax", "glm"]
    out = _run(
        server._handle_conference(
            {"question": "how do we scale this?", "models": bench, "rounds": 2}
        )
    )
    assert out["premise"]["claim"] == "the write path is the bottleneck"
    # `Participant 1` is the first NON-rapporteur seat: the rapporteur (fable) is
    # anonymized last, so the legend has to be applied to read the reply.
    assert out["premise"]["asserted_by"] == [oracles.label("minimax")]
    # the attacker is a seat that did NOT assert it
    assert out["premise"]["attacker"] not in out["premise"]["asserted_by"]
    assert "the write path is the bottleneck" in seen["attack_prompt"]
    # it lands as a round-1 addendum, tagged, without renumbering the open rounds
    attacks = [p for p in out["posts"] if p.get("role") == "premise_attack"]
    assert len(attacks) == 1 and attacks[0]["round"] == 1
    assert "premise attack" in out["transcript"][-1] or any(
        "premise attack" in line for line in out["transcript"]
    )
    # the premise turn reads the openings anonymized, like the rapporteur
    assert "Participant 1" in seen["premise_prompt"]
    for key in bench:
        assert f"[{oracles.label(key)} · " not in seen["premise_prompt"]


def test_c6_when_everyone_asserted_it_the_last_seat_attacks(monkeypatch):
    """The usual case, and the interesting one. The attacker must not habitually be
    the first speaker or the rapporteur."""
    _allow(monkeypatch)
    _stub_premise(
        monkeypatch,
        "CLAIM: caching is the answer\nASSERTED_BY: Participant 1, Participant 2, Participant 3",
    )
    bench = ["fable", "minimax", "glm"]
    out = _run(
        server._handle_conference(
            {"question": "how do we scale this?", "models": bench, "rounds": 2}
        )
    )
    assert out["premise"]["attacker"] == oracles.label(bench[-1])


def test_c6_an_unparseable_premise_skips_cleanly(monkeypatch):
    """No claim means nothing to attack. Inventing one would hand a model a premise
    no participant actually held."""
    _allow(monkeypatch)
    _stub_premise(monkeypatch, "I could not identify a shared premise, sorry.")
    out = _run(
        server._handle_conference(
            {
                "question": "how do we scale this?",
                "models": ["fable", "minimax", "glm"],
                "rounds": 2,
            }
        )
    )
    assert "premise" not in out
    assert any(e.get("kind") == "premise_unavailable" for e in out["errors"])
    assert not [p for p in out["posts"] if p.get("role") == "premise_attack"]
    assert out["status"] == "ok"  # a missing premise is not a failed conference


def test_c6_opting_out_restores_the_old_call_count(monkeypatch):
    _allow(monkeypatch)
    calls: list[str] = []

    async def fake_run(key, question, context="", **kw):
        calls.append(key)
        if "MAP OF THE DISAGREEMENT" in question:
            return OracleResult("ok", key=key, text="CONVERGED: x", model=key)
        return OracleResult("ok", key=key, text=f"{key} opens", model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    args = {
        "question": "how do we scale this?",
        "models": ["fable", "minimax", "glm"],
        "rounds": 2,
        "attack_premise": False,
    }
    out = _run(server._handle_conference(dict(args)))
    assert "premise" not in out
    assert len(calls) == 3 * 2 + 1  # seats x rounds + rapporteur, exactly as before


def test_c6_is_skipped_on_a_two_seat_bench(monkeypatch):
    """"all but one of them" is meaningless with two seats."""
    _allow(monkeypatch)
    _stub_premise(monkeypatch, "CLAIM: x\nASSERTED_BY: Participant 1")
    out = _run(
        server._handle_conference(
            {"question": "t?", "models": ["fable", "minimax"], "rounds": 2}
        )
    )
    assert "premise" not in out


def test_c6_parse_premise_is_forgiving_about_everything_but_the_claim():
    assert conference._parse_premise("CLAIM: a\nASSERTED_BY: P1, P2") == ("a", ["P1", "P2"])
    assert conference._parse_premise("claim: a") == ("a", [])  # case-insensitive
    assert conference._parse_premise("CLAIM: a") == ("a", [])  # asserted_by optional
    assert conference._parse_premise("CLAIM:   ") == ("", [])  # empty claim is no claim
    assert conference._parse_premise("no structure here") == ("", [])
    assert conference._parse_premise("") == ("", [])


# --- review findings on this branch (PR #102) --------------------------------


def test_review1_an_unresolvable_asserted_by_does_not_hand_the_attack_to_the_rapporteur(
    monkeypatch,
):
    """`_parse_premise` allows a claim with no ASSERTED_BY, and a model may answer
    "all of them". Either way we learned nothing about WHO asserted it — not that
    nobody did, since the claim was selected for being shared. Falling through to
    the first speaker handed the attack to the seat that is also the rapporteur."""
    _allow(monkeypatch)
    for reply in (
        "CLAIM: caching is the answer",
        "CLAIM: caching is the answer\nASSERTED_BY: all of them",
        "CLAIM: caching is the answer\nASSERTED_BY: ",
    ):
        _stub_premise(monkeypatch, reply)
        bench = ["fable", "minimax", "glm"]  # `fable` is the default rapporteur
        out = _run(
            server._handle_conference({"question": "t?", "models": bench, "rounds": 2})
        )
        attacker = out["premise"]["attacker"]
        assert attacker == oracles.label(bench[-1]), reply
        assert attacker != oracles.label("fable"), "the rapporteur attacked its own premise"


def test_review2_seats_sharing_a_label_are_not_collapsed(monkeypatch):
    """`fable` and `fable51` share a label, as do atlas:/openrouter: pairs. Keyed on
    the label, two independent openings read to the rapporteur as one participant
    contradicting itself, and the legend reported fewer seats than there were."""
    _allow(monkeypatch)
    _stub_run(monkeypatch)
    bench = ["fable", "fable51", "minimax"]
    out = _run(server._handle_conference({"question": "t?", "models": bench, "rounds": 1}))
    legend = out["map_legend"]
    assert len(legend) == len(bench), f"a seat vanished from the legend: {legend}"
    assert set(legend) == {f"Participant {i + 1}" for i in range(len(bench))}


def test_review3_the_attacker_sees_the_shared_context(monkeypatch):
    """Every other turn sees it. An attacker arguing about code it cannot see is the
    very defect this release removes from the council synthesizer."""
    _allow(monkeypatch)
    seen = _stub_premise(monkeypatch, "CLAIM: x\nASSERTED_BY: Participant 1")
    _run(
        server._handle_conference(
            {
                "question": "t?",
                "models": ["fable", "minimax", "glm"],
                "rounds": 2,
                "context": "MARKER_shared_code()",
            }
        )
    )
    assert "MARKER_shared_code()" in seen["attack_prompt"]


def test_review4_an_elided_map_says_so(monkeypatch):
    """The caller sees the whole argument in `transcript`, so a map written on less
    than that has to say so."""
    _allow(monkeypatch)
    monkeypatch.setattr(conference, "_TRANSCRIPT_BUDGET", 400)

    async def fake_run(key, question, context="", **kw):
        if "MAP OF THE DISAGREEMENT" in question:
            return OracleResult("ok", key=key, text="CONVERGED: x", model=key)
        return OracleResult("ok", key=key, text="z" * 300, model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    out = _run(
        server._handle_conference(
            {
                "question": "t?",
                "models": ["fable", "minimax"],
                "rounds": 3,
                "attack_premise": False,
            }
        )
    )
    assert any(e.get("kind") == "transcript_elided" for e in out["errors"])


def test_review5_a_stringified_false_still_opts_out(monkeypatch):
    """`is not False` honoured only a real JSON false, so a client that stringifies
    booleans paid for the two calls it asked to skip."""
    _allow(monkeypatch)
    for falsey in (False, "false", "False", 0, "0"):
        calls: list[str] = []

        async def fake_run(key, question, context="", _c=calls, **kw):
            _c.append(key)
            if "MAP OF THE DISAGREEMENT" in question:
                return OracleResult("ok", key=key, text="CONVERGED: x", model=key)
            return OracleResult("ok", key=key, text="pos", model=key)

        monkeypatch.setattr(oracles, "run", fake_run)
        out = _run(
            server._handle_conference(
                {
                    "question": "t?",
                    "models": ["fable", "minimax", "glm"],
                    "rounds": 2,
                    "attack_premise": falsey,
                }
            )
        )
        assert "premise" not in out, falsey
        assert len(calls) == 3 * 2 + 1, falsey


def test_review6_a_missing_premise_is_not_a_silent_seat(monkeypatch):
    """`errors` is read as 'which seat fell silent when'; the synthesizer spoke
    normally, it just found no premise."""
    _allow(monkeypatch)
    _stub_premise(monkeypatch, "no structure here")
    out = _run(
        server._handle_conference(
            {"question": "t?", "models": ["fable", "minimax", "glm"], "rounds": 2}
        )
    )
    entry = next(e for e in out["errors"] if e["kind"] == "premise_unavailable")
    assert "round" not in entry and entry["stage"] == "premise"


def test_review2_a_duplicate_label_bench_leaks_nothing_to_the_rapporteur(monkeypatch):
    """Anonymizing by matching label TEXT cannot tell two seats that share a label
    apart: one keeps its real name or both collapse into one pseudonym. Rewriting by
    the seat that wrote the line handles it exactly."""
    _allow(monkeypatch)
    seen: dict[str, str] = {}

    async def fake_run(key, question, context="", **kw):
        if "MAP OF THE DISAGREEMENT" in question:
            seen["map"] = question
            return OracleResult("ok", key=key, text="CONVERGED: x", model=key)
        return OracleResult("ok", key=key, text=f"{key} opens", model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    bench = ["fable", "fable51", "minimax"]  # fable and fable51 share a label
    out = _run(server._handle_conference({"question": "t?", "models": bench, "rounds": 1}))
    for key in bench:
        assert f"[{oracles.label(key)} · " not in seen["map"], f"{key} named to the rapporteur"
    assert len(out["map_legend"]) == len(bench)
    assert out["transcript"][1].startswith(f"[{oracles.label('fable')} · ")  # caller's view
