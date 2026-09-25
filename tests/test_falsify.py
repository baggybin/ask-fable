"""ask_falsify handler — the stateful clerk loop end to end.

Drives ``falsify._handle_falsify`` with ``oracles.run`` stubbed (canned json-falsify
blocks), the context bus mocked in-memory, and the guard allowed. No model is called and
nothing touches disk. Covers: the clerk killing a fabricated cite, a real cite surviving,
cross-call persistence + the no-reassert rule, the different-lab guard, and refusals.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import ask_fable.server as server
from ask_fable import (
    audit,
    context_store,
    falsify,
    falsify_metrics,
    oracles,
    outputs,
    sandbox,
    worker,
)
from ask_fable.oracle_common import OracleResult


def _run(coro):
    return asyncio.run(coro)


def _wrap(block: dict) -> str:
    return "prose here\n\n```json-falsify\n" + json.dumps(block) + "\n```"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setenv(
        "ASK_FABLE_REPUTATION_PATH", str(tmp_path / "rep.db")
    )  # never touch the real store
    monkeypatch.setattr(server.guard, "check", lambda q, c="", **kw: (True, ""))
    monkeypatch.setattr(audit, "record", lambda **k: None)
    monkeypatch.setattr(outputs, "save", lambda **k: "/tmp/fake-transcript")
    monkeypatch.setattr(server, "_hub_mirror", lambda **k: None)
    store: dict[str, str] = {}
    versions: dict[str, int] = {}

    def _put(k, v, description="", expected_version=None):
        cur = versions.get(k, 0)
        if expected_version is not None and expected_version != cur:
            return False  # optimistic-concurrency conflict
        store[k] = v
        versions[k] = cur + 1
        return True

    monkeypatch.setattr(context_store, "put", _put)
    monkeypatch.setattr(context_store, "get", lambda k: store.get(k))
    monkeypatch.setattr(
        context_store, "get_versioned",
        lambda k: (store[k], versions.get(k, 0)) if k in store else None,
    )
    monkeypatch.setattr(context_store, "last_error", lambda: None)  # the mock store never degrades
    return store


def _stub(monkeypatch, assert_blocks, falsify_blocks):
    """Queue per-role canned turns; the loop pops one assert + one falsify per round."""
    calls: list[str] = []

    async def fake_run(key, question, context="", **kw):
        calls.append(key)
        if "You are the FALSIFIER" in question:
            blk = falsify_blocks.pop(0) if falsify_blocks else {"role": "falsify", "attacks": []}
        else:
            blk = assert_blocks.pop(0) if assert_blocks else {"role": "assert", "claims": []}
        return OracleResult("ok", key=key, text=_wrap(blk), model=key)

    monkeypatch.setattr(oracles, "run", fake_run)
    return calls


CTX = "The API spec: writes are idempotent."


def test_fabricated_cite_is_killed_real_cite_withstands(monkeypatch):
    assert_blocks = [
        {
            "role": "assert",
            "claims": [
                {
                    "id": "H1",
                    "domain": "api",
                    "claim": "writes are idempotent",
                    "receipts": [{"kind": "cite", "quote": "writes are idempotent"}],
                },
                {
                    "id": "H2",
                    "domain": "api",
                    "claim": "reads are cached",
                    "receipts": [{"kind": "cite", "quote": "reads are cached forever"}],
                },  # NOT in CTX
            ],
        }
    ]
    falsify_blocks = [
        {
            "role": "falsify",
            "attacks": [
                {"target": "H2", "move": "challenge"},  # unsupported → dies
                {"target": "H1", "move": "challenge"},  # supported → withstands
            ],
        }
    ]
    _stub(monkeypatch, assert_blocks, falsify_blocks)
    out = _run(
        falsify._handle_falsify(
            {"question": "is the API idempotent?", "session": "t1", "context": CTX}
        )
    )
    assert out["status"] == "ok" and out["round"] == 1
    assert out["falsify"]["killed"] == ["H2"]
    assert "H1" in out["falsify"]["open"]  # supported, one failed challenge (< K)


def test_supported_claim_survives_after_k_failed_challenges(monkeypatch):
    assert_blocks = [
        {
            "role": "assert",
            "claims": [
                {
                    "id": "H1",
                    "domain": "api",
                    "claim": "writes are idempotent",
                    "receipts": [{"kind": "cite", "quote": "writes are idempotent"}],
                },
            ],
        },
        {"role": "assert", "claims": []},  # round 2: nothing new
    ]
    falsify_blocks = [
        {"role": "falsify", "attacks": [{"target": "H1", "move": "challenge"}]},
        {"role": "falsify", "attacks": [{"target": "H1", "move": "challenge"}]},
    ]
    _stub(monkeypatch, assert_blocks, falsify_blocks)
    out = _run(
        falsify._handle_falsify(
            {"question": "idempotent?", "session": "t2", "context": CTX, "rounds": 2}
        )
    )
    assert out["round"] == 2 and out["falsify"]["survived"] == ["H1"]


def test_persistence_and_no_reassert_across_calls(monkeypatch):
    # call 1 kills H2 (fabricated cite)
    _stub(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {
                        "id": "H2",
                        "domain": "api",
                        "claim": "reads are cached",
                        "receipts": [{"kind": "cite", "quote": "not present"}],
                    }
                ],
            }
        ],
        [{"role": "falsify", "attacks": [{"target": "H2", "move": "challenge"}]}],
    )
    out1 = _run(falsify._handle_falsify({"question": "q", "session": "shared", "context": CTX}))
    assert out1["falsify"]["killed"] == ["H2"]

    # call 2 (same session) tries to re-assert the killed H2 bare + adds H3 with a real cite
    _stub(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {"id": "H2", "domain": "api", "claim": "reads are cached (again)"},
                    {
                        "id": "H3",
                        "domain": "api",
                        "claim": "writes are idempotent",
                        "receipts": [{"kind": "cite", "quote": "writes are idempotent"}],
                    },
                ],
            }
        ],
        [{"role": "falsify", "attacks": []}],
    )
    out2 = _run(falsify._handle_falsify({"question": "q", "session": "shared", "context": CTX}))
    assert out2["round"] == 2  # ledger advanced, not replayed
    assert out2["falsify"]["killed"] == ["H2"]  # H2 stayed dead (bare re-assert dropped)
    assert "H3" in out2["falsify"]["open"]  # the new claim landed


def test_different_lab_guard_is_bad_args(monkeypatch):
    calls = _stub(monkeypatch, [], [])
    out = _run(
        falsify._handle_falsify(
            {
                "question": "q",
                "session": "s",
                "context": CTX,
                "assertor": "fable",
                "falsifier": "opus",
            }
        )
    )
    assert out["status"] == "error" and out["kind"] == "bad_args" and "lab" in out["detail"].lower()
    assert calls == []  # rejected before any model turn


def test_second_call_on_locked_session_is_busy(monkeypatch):
    # F2a: same-session runs are serialized; a second overlapping call fails fast with
    # session_busy rather than clobbering the first's ledger or blocking for minutes.
    _stub(monkeypatch, [], [])
    monkeypatch.setattr(falsify, "_FALSIFY_LOCK_WAIT_S", 0.01)  # don't wait 2s in the test

    async def scenario():
        lock = server._session_lock("falsify:held")
        async with lock:  # stand in for an in-flight run holding the session
            return await falsify._handle_falsify(
                {"question": "q", "session": "held", "context": CTX,
                 "assertor": "fable", "falsifier": "minimax"}
            )

    out = _run(scenario())
    assert out["status"] == "error" and out["kind"] == "session_busy"


def test_anthropic_variant_falsifier_is_same_lab(monkeypatch):
    # WI-2: sonnet grading fable is same-lab (both Anthropic). falsify's old local _LAB
    # table missed the variants; lab identity now comes from oracles.lab_of (one source),
    # which maps sonnet/opus48/fable51 → anthropic.
    calls = _stub(monkeypatch, [], [])
    out = _run(
        falsify._handle_falsify(
            {
                "question": "q",
                "session": "s",
                "context": CTX,
                "assertor": "fable",
                "falsifier": "sonnet",
            }
        )
    )
    assert out["status"] == "error" and out["kind"] == "bad_args" and "lab" in out["detail"].lower()
    assert calls == []  # rejected before any model turn


def test_parse_verdict_word_boundary_and_negation():
    # ED-1: a plain substring scan read the "TRUE" inside "UNTRUE" and certified a refuted
    # claim as stable. Word boundaries + a negation guard fix it.
    assert falsify._parse_verdict("TRUE") == "TRUE"
    assert falsify._parse_verdict("FALSE") == "FALSE"
    assert falsify._parse_verdict("UNSURE") == "UNSURE"
    assert falsify._parse_verdict("UNTRUE: it contradicts the premise") == "FALSE"
    assert falsify._parse_verdict("NOT TRUE") == "FALSE"
    assert falsify._parse_verdict("The claim is FALSE, not TRUE") == "FALSE"
    assert falsify._parse_verdict("gibberish with no verdict") == "UNSURE"


def test_missing_session_is_bad_args(monkeypatch):
    _stub(monkeypatch, [], [])
    out = _run(falsify._handle_falsify({"question": "q", "context": CTX}))
    assert out["status"] == "error" and out["kind"] == "bad_args" and "session" in out["detail"]


def test_guard_denied_refuses_without_a_model(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="", **kw: (False, "prohibited_x"))
    calls = _stub(monkeypatch, [], [])
    out = _run(falsify._handle_falsify({"question": "blocked", "session": "s", "context": CTX}))
    assert out["status"] == "refused" and out["reason"] == "prohibited_x"
    assert calls == []


def test_run_arena_scores_each_item(monkeypatch):
    _stub(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {
                        "id": "H1",
                        "domain": "api",
                        "claim": "reads are cached",
                        "receipts": [{"kind": "cite", "quote": "not in the pack"}],
                    }
                ],
            }
        ],
        [{"role": "falsify", "attacks": [{"target": "H1", "move": "challenge"}]}],
    )
    results = _run(
        falsify.run_arena([{"question": "cached?", "session": "arena1", "context": CTX}])
    )
    assert len(results) == 1
    r = results[0]
    assert r["status"] == "ok" and r["session"] == "arena1"
    assert r["killed"] == ["H1"]
    assert r["verdict"] == "retrieval-bound"  # v1 cite kill is pre-loaded
    assert r["ablation"]["prevented"] == 1  # the clerk killed what consensus would accept


def _stub_sandbox(monkeypatch, verdicts: dict[str, tuple[str, int | None]]):
    """Make the sandbox deterministic for clerk run: tests. `verdicts` maps a marker
    substring in the snippet to a (status, returncode) SandboxResult. No real exec."""
    monkeypatch.setattr(sandbox, "enabled", lambda: True)
    monkeypatch.setattr(sandbox, "available", lambda: True)

    async def _self_test():
        return True

    async def _run_python(code):
        for marker, (status, rc) in verdicts.items():
            if marker in code:
                return sandbox.SandboxResult(status, rc, "out", "err")
        return sandbox.SandboxResult("ok", 0, "", "")

    monkeypatch.setattr(sandbox, "self_test", _self_test)
    monkeypatch.setattr(sandbox, "run_python", _run_python)


def test_assertor_run_receipt_supports_and_survives(monkeypatch):
    _stub_sandbox(monkeypatch, {"PASS": ("ok", 0)})
    _stub(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {
                        "id": "H1",
                        "domain": "algo",
                        "claim": "sorted works",
                        "receipts": [
                            {"kind": "run", "code": "assert sorted([2,1])==[1,2]  # PASS"}
                        ],
                    }
                ],
            },
            {"role": "assert", "claims": []},
        ],
        [
            {"role": "falsify", "attacks": [{"target": "H1", "move": "challenge"}]},
            {"role": "falsify", "attacks": [{"target": "H1", "move": "challenge"}]},
        ],
    )
    out = _run(
        falsify._handle_falsify({"question": "q", "session": "run1", "context": "c", "rounds": 2})
    )
    assert out["falsify"]["survived"] == ["H1"]  # run-passed → supported → withstands K challenges


def test_falsifier_run_kill_is_constructed(monkeypatch):
    _stub_sandbox(monkeypatch, {"FAILTEST": ("fail", 1)})
    _stub(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {
                        "id": "H1",
                        "domain": "algo",
                        "claim": "x",
                        "receipts": [{"kind": "cite", "quote": "x"}],
                    }
                ],
            }
        ],
        [
            {
                "role": "falsify",
                "attacks": [
                    {"target": "H1", "move": "run", "code": "raise SystemExit(1)  # FAILTEST"}
                ],
            }
        ],
    )
    out = _run(falsify._handle_falsify({"question": "q", "session": "run2", "context": "x"}))
    assert out["falsify"]["killed"] == ["H1"]
    ledger, _version = falsify._load_ledger("run2")
    assert falsify_metrics.kill_provenance(ledger)["constructed"] == 1
    assert falsify_metrics.grep_ratio(ledger) < 1.0  # a manufactured kill clears the frontier bar


def test_falsifier_run_pass_does_not_kill(monkeypatch):
    _stub_sandbox(monkeypatch, {"PASSTEST": ("ok", 0)})
    _stub(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {
                        "id": "H1",
                        "domain": "algo",
                        "claim": "x",
                        "receipts": [{"kind": "cite", "quote": "x"}],
                    }
                ],
            }
        ],
        [
            {
                "role": "falsify",
                "attacks": [{"target": "H1", "move": "run", "code": "pass  # PASSTEST"}],
            }
        ],
    )
    out = _run(falsify._handle_falsify({"question": "q", "session": "run3", "context": "x"}))
    assert out["falsify"]["killed"] == [] and "H1" in out["falsify"]["open"]


def test_run_receipts_inconclusive_and_inert_when_disabled(monkeypatch):
    monkeypatch.setattr(sandbox, "enabled", lambda: False)  # opt-in OFF
    called: list[str] = []

    async def _rp(code):
        called.append(code)
        return sandbox.SandboxResult("ok", 0, "", "")

    monkeypatch.setattr(sandbox, "run_python", _rp)
    _stub(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {
                        "id": "H1",
                        "domain": "algo",
                        "claim": "x",
                        "receipts": [{"kind": "run", "code": "assert True"}],
                    }
                ],
            }
        ],
        [
            {
                "role": "falsify",
                "attacks": [{"target": "H1", "move": "run", "code": "raise SystemExit(1)"}],
            }
        ],
    )
    out = _run(falsify._handle_falsify({"question": "q", "session": "run4", "context": "x"}))
    assert out["falsify"]["killed"] == []  # a run attack can't kill when execution is off
    assert called == []  # and the sandbox is never even invoked


def _stub_meta(monkeypatch, assert_blocks, falsify_blocks, judge):
    """Stub the Haiku worker (the metamorph paraphraser) and oracles.run (the cold judge
    + the assert/falsify turns)."""

    async def fake_worker(prompt, context="", **kw):
        return OracleResult("ok", key="haiku", text="a restated claim", model="claude-haiku-4-5")

    async def fake_run(key, question, context="", **kw):
        if "Judge the following proposition" in question:
            return OracleResult("ok", key=key, text=judge, model=key)
        if "You are the FALSIFIER" in question:
            blk = falsify_blocks.pop(0) if falsify_blocks else {"role": "falsify", "attacks": []}
        else:
            blk = assert_blocks.pop(0) if assert_blocks else {"role": "assert", "claims": []}
        return OracleResult(
            "ok", key=key, text="p\n\n```json-falsify\n" + json.dumps(blk) + "\n```", model=key
        )

    monkeypatch.setattr(worker, "run", fake_worker)
    monkeypatch.setattr(oracles, "run", fake_run)


def test_paraphrase_prefers_the_haiku_worker(monkeypatch):
    async def fake_worker(prompt, context="", **kw):
        return OracleResult(
            "ok", key="haiku", text="a haiku reword\nsecond line", model="claude-haiku-4-5"
        )

    async def no_oracle(*a, **k):
        raise AssertionError("must not fall back to an oracle when Haiku answers")

    monkeypatch.setattr(worker, "run", fake_worker)
    monkeypatch.setattr(oracles, "run", no_oracle)
    para = _run(falsify._paraphrase("some claim", assertor="fable"))
    assert para == "a haiku reword"  # first line only


def test_paraphrase_falls_back_to_cross_lab_when_haiku_fails(monkeypatch):
    async def bad_worker(prompt, context="", **kw):
        return OracleResult("error", key="haiku", kind="model_unavailable", text="too old")

    async def fake_run(key, question, context="", **kw):
        return OracleResult("ok", key=key, text=f"{key} reword", model=key)

    monkeypatch.setattr(worker, "run", bad_worker)
    monkeypatch.setattr(oracles, "available", lambda k: k == "minimax")
    monkeypatch.setattr(oracles, "run", fake_run)
    para = _run(falsify._paraphrase("some claim", assertor="fable"))
    assert para == "minimax reword"  # the cheap cross-lab fallback, not the assertor


def test_paraphrase_never_self_perturbs(monkeypatch):
    async def bad_worker(prompt, context="", **kw):
        return OracleResult("error", key="haiku", kind="model_unavailable", text="too old")

    async def no_oracle(key, question, context="", **kw):
        raise AssertionError(f"no perturber should have run (got {key})")

    monkeypatch.setattr(worker, "run", bad_worker)
    monkeypatch.setattr(oracles, "available", lambda k: False)  # nothing configured
    monkeypatch.setattr(oracles, "run", no_oracle)
    # None -> the check skips this round rather than let the assertor reword its own claim
    assert _run(falsify._paraphrase("some claim", assertor="fable")) is None


def test_metamorph_stable_lets_unbacked_claim_survive(monkeypatch):
    _stub_meta(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {
                        "id": "H1",
                        "domain": "phil",
                        "claim": "an abstract claim",
                        "receipts": [{"kind": "unbacked"}],
                    }
                ],
            },
            {"role": "assert", "claims": []},
        ],
        [
            {"role": "falsify", "attacks": [{"target": "H1", "move": "challenge"}]},
            {"role": "falsify", "attacks": [{"target": "H1", "move": "challenge"}]},
        ],
        judge="TRUE",
    )
    out = _run(
        falsify._handle_falsify(
            {"question": "q", "session": "m1", "context": "", "rounds": 2, "metamorph": True}
        )
    )
    assert out["falsify"]["survived"] == ["H1"]  # stable metamorph = weak support
    assert out["falsify"]["stable_unverified"] == ["H1"]  # ...but flagged, not sold as truth


def test_metamorph_unstable_leaves_claim_unsupported(monkeypatch):
    _stub_meta(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {"id": "H1", "domain": "phil", "claim": "c", "receipts": [{"kind": "unbacked"}]}
                ],
            }
        ],
        [{"role": "falsify", "attacks": [{"target": "H1", "move": "challenge"}]}],
        judge="FALSE",
    )
    out = _run(
        falsify._handle_falsify(
            {"question": "q", "session": "m2", "context": "", "metamorph": True}
        )
    )
    assert out["falsify"]["killed"] == ["H1"]  # unstable → unsupported → the challenge kills it
    assert out["falsify"]["stable_unverified"] == []


def test_metamorph_off_makes_no_extra_calls(monkeypatch):
    judged: list[str] = []
    paraphrased: list[str] = []

    async def fake_worker(prompt, context="", **kw):
        paraphrased.append(prompt)
        return OracleResult("ok", key="haiku", text="a restated claim", model="claude-haiku-4-5")

    async def fake_run(key, question, context="", **kw):
        if "Judge the following proposition" in question:
            judged.append(question)
            return OracleResult("ok", key=key, text="TRUE", model=key)
        block = (
            {"role": "falsify", "attacks": []}
            if "You are the FALSIFIER" in question
            else {
                "role": "assert",
                "claims": [
                    {"id": "H1", "domain": "phil", "claim": "c", "receipts": [{"kind": "unbacked"}]}
                ],
            }
        )
        return OracleResult(
            "ok", key=key, text="p\n\n```json-falsify\n" + json.dumps(block) + "\n```", model=key
        )

    monkeypatch.setattr(worker, "run", fake_worker)
    monkeypatch.setattr(oracles, "run", fake_run)
    out = _run(
        falsify._handle_falsify({"question": "q", "session": "m3", "context": ""})
    )  # metamorph off
    assert judged == [] and paraphrased == []  # no paraphrase/judge calls when the flag is off
    assert "H1" in out["falsify"]["open"]  # unbacked + unchallenged → stays open


def test_tool_registered_with_schema():
    assert isinstance(server.ASK_FALSIFY_TOOL_DESCRIPTION, str)
    props = server._FALSIFY_SCHEMA["properties"]
    assert {"session", "assertor", "falsifier", "rounds"} <= set(props)
    assert server._FALSIFY_SCHEMA["required"] == ["question", "session"]


# --- degraded-store guard: never start a fresh ledger over a maybe-intact one --


def test_degraded_store_refuses_fresh_ledger(monkeypatch):
    monkeypatch.setattr(context_store, "get", lambda k: None)
    monkeypatch.setattr(context_store, "last_error", lambda: "OperationalError: database is locked")
    calls = _stub(monkeypatch, [], [])
    out = _run(falsify._handle_falsify({"question": "q", "session": "deg", "context": CTX}))
    assert out["status"] == "error" and out["kind"] == "ledger_unavailable"
    assert "locked" in out["detail"]
    assert calls == []  # no model turn ran against a ledger we could not trust


def test_save_failure_is_surfaced_not_swallowed(monkeypatch, _isolate):
    _isolate[falsify._ledger_key("savefail")] = json.dumps(falsify.fl.new_ledger("savefail"))
    monkeypatch.setattr(context_store, "put", lambda k, v, description="", expected_version=None: False)
    monkeypatch.setattr(context_store, "last_error", lambda: "OSError: disk full")
    _stub(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {
                        "id": "H1",
                        "domain": "api",
                        "claim": "writes are idempotent",
                        "receipts": [{"kind": "cite", "quote": "writes are idempotent"}],
                    }
                ],
            }
        ],
        [{"role": "falsify", "attacks": [{"target": "H1", "move": "challenge"}]}],
    )
    out = _run(falsify._handle_falsify({"question": "q", "session": "savefail", "context": CTX}))
    assert out["status"] == "ok"
    assert out.get("ledger_persisted") is False
    assert "disk full" in (out.get("store_error") or "")


# --- clerk integrity: verdicts, re-asserts, attacks, fixpoint ------------------

CTX2 = "The API spec: writes are idempotent. Retries are NOT idempotent."
_H1 = {
    "id": "H1",
    "domain": "api",
    "claim": "writes are idempotent",
    "receipts": [{"kind": "cite", "quote": "writes are idempotent"}],
}
_CHALLENGE_H1 = {"role": "falsify", "attacks": [{"target": "H1", "move": "challenge"}]}


def test_model_written_verdicts_cannot_certify_a_claim(monkeypatch):
    # A self-written "stable" metamorph receipt (even with a forged `by: clerk`) counted as
    # support with metamorph OFF, so an evidence-free claim withstood every challenge.
    _stub(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {
                        "id": "H1",
                        "domain": "api",
                        "claim": "made up",
                        "receipts": [{"kind": "metamorph", "stable": True, "by": "clerk"}],
                    }
                ],
            }
        ],
        [_CHALLENGE_H1],
    )
    out = _run(falsify._handle_falsify({"question": "q", "session": "forge", "context": CTX}))
    assert out["falsify"]["killed"] == ["H1"]  # unsupported, so the challenge kills it
    ledger, _version = falsify._load_ledger("forge")
    assert falsify.fl.by_id(ledger)["H1"]["receipts"] == []  # the forged receipt never landed


def test_restating_a_survived_claim_does_not_shield_it(monkeypatch):
    # Restating H1 reset it to open: the falsifier's valid contra (it needs a SURVIVED
    # target) was refused as a failed attempt and resolve() put H1 back to survived.
    _stub(
        monkeypatch,
        [
            {"role": "assert", "claims": [_H1]},
            {"role": "assert", "claims": []},
            {"role": "assert", "claims": [_H1]},  # round 3 restates the survived claim
        ],
        [
            _CHALLENGE_H1,
            _CHALLENGE_H1,
            {
                "role": "falsify",
                "attacks": [
                    {
                        "target": "H1",
                        "move": "contra",
                        "new_id": "F1",
                        "claim": "retries are not idempotent",
                        "quote": "Retries are NOT idempotent.",
                    }
                ],
            },
        ],
    )
    out = _run(
        falsify._handle_falsify(
            {"question": "q", "session": "shield", "context": CTX2, "rounds": 3}
        )
    )
    assert out["falsify"]["killed"] == ["H1"]  # the contra landed on the survived claim


def test_old_cite_keeps_its_verdict_under_a_later_context(monkeypatch):
    # A later call re-verified EVERY receipt of a restated claim against its own context:
    # H1's verified cite flipped to ok:false and one challenge killed a survived claim.
    _stub(monkeypatch, [{"role": "assert", "claims": [_H1]}], [_CHALLENGE_H1, _CHALLENGE_H1])
    out1 = _run(
        falsify._handle_falsify({"question": "q", "session": "rev", "context": CTX, "rounds": 2})
    )
    assert out1["falsify"]["survived"] == ["H1"]
    restated = {**_H1, "receipts": [{"kind": "unbacked"}]}
    _stub(monkeypatch, [{"role": "assert", "claims": [restated]}], [_CHALLENGE_H1])
    out2 = _run(
        falsify._handle_falsify({"question": "q", "session": "rev", "context": "another pack"})
    )
    assert out2["falsify"]["survived"] == ["H1"]  # the challenge fails against the old cite


def test_attacks_on_a_killed_claim_are_ignored(monkeypatch):
    # Every challenge on an already-dead claim was accepted again — another kill and another
    # kills/deaths bump each time, inflating kill_provenance and spark_density.
    challenge = {"target": "H1", "move": "challenge"}
    _stub(
        monkeypatch,
        [
            {
                "role": "assert",
                "claims": [
                    {
                        "id": "H1",
                        "domain": "api",
                        "claim": "reads are cached",
                        "receipts": [{"kind": "cite", "quote": "not present"}],
                    }
                ],
            }
        ],
        [
            {"role": "falsify", "attacks": [challenge] * 3},
            {"role": "falsify", "attacks": [challenge]},
        ],
    )
    out = _run(
        falsify._handle_falsify({"question": "q", "session": "rekill", "context": CTX, "rounds": 2})
    )
    assert out["falsify"]["killed"] == ["H1"]
    ledger, _version = falsify._load_ledger("rekill")
    assert len(falsify.fl.by_id(ledger)["H1"]["kills"]) == 1
    assert falsify_metrics.kill_provenance(ledger)["total"] == 1
    rep = out["falsify"]["rep"].values()
    assert sum(b["kills"] for b in rep) == 1 and sum(b["deaths"] for b in rep) == 1


def test_contra_must_register_a_new_claim():
    # A contra naming an EXISTING id is a failed attempt. A killed id's re-assert is rejected,
    # yet its edge still killed the target; an open id got the falsifier's quote grafted onto
    # the assertor's own claim (making it compoundable).
    ledger = falsify.fl.new_ledger("s")
    ledger["claims"] = [
        {
            "id": "H1",
            "author": "A",
            "domain": "api",
            "claim": "writes are idempotent",
            "status": "survived",
            "attempts": 2,
            "kills": [],
            "receipts": [{"kind": "cite", "quote": "writes are idempotent", "ok": True}],
        },
        {
            "id": "H2",
            "author": "A",
            "domain": "api",
            "claim": "old",
            "status": "killed",
            "attempts": 0,
            "receipts": [],
            "kills": [{"by": "B", "kind": "challenge", "round": 1, "accepted": True}],
        },
        {
            "id": "H3",
            "author": "A",
            "domain": "api",
            "claim": "vibes",
            "status": "open",
            "attempts": 0,
            "kills": [],
            "receipts": [{"kind": "unbacked"}],
        },
    ]
    block = {
        "attacks": [
            {
                "target": "H1",
                "move": "contra",
                "new_id": new_id,
                "claim": "retries differ",
                "quote": "Retries are NOT idempotent.",
            }
            for new_id in ("H2", "H3")
        ]
    }
    out = falsify.fl.resolve(_run(falsify._clerk_attacks(ledger, block, "B", CTX2, 3, False)))
    ids = falsify.fl.by_id(out)
    assert ids["H1"]["status"] == "survived" and ids["H1"]["attempts"] == 4  # two failed tries
    assert out["edges"] == [] and ids["H2"]["claim"] == "old"
    assert ids["H3"]["receipts"] == [{"kind": "unbacked"}]  # nothing grafted onto H3


def test_fixpoint_status_resets_when_a_later_call_moves_the_ledger(monkeypatch):
    # Once a session reached `fixpoint`, every later call reported (and persisted)
    # `fixpoint` — even one that added a claim and killed another.
    unbacked = {"id": "H1", "domain": "api", "claim": "c1", "receipts": [{"kind": "unbacked"}]}
    _stub(monkeypatch, [{"role": "assert", "claims": [unbacked]}], [])
    out1 = _run(
        falsify._handle_falsify({"question": "q", "session": "fx", "context": CTX, "rounds": 2})
    )
    assert out1["ledger_status"] == "fixpoint"  # round 2 changed nothing
    _stub(monkeypatch, [{"role": "assert", "claims": [{**_H1, "id": "H2"}]}], [_CHALLENGE_H1])
    out2 = _run(falsify._handle_falsify({"question": "q", "session": "fx", "context": CTX}))
    assert out2["falsify"]["killed"] == ["H1"] and out2["ledger_status"] == "active"
    ledger, _version = falsify._load_ledger("fx")
    assert ledger["status"] == "active"
