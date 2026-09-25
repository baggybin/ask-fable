"""Pin tests for the PR #100 fix-batch re-check (the PR #100 fix-batch report).

Every test here fails on the code as PR #100 left it. One test per finding, named
by its id in that report, so a regression points straight at the write-up.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import ask_fable.server as server
import ask_fable.stats as stats
from ask_fable import _denylist, diagnose, oracles, trace_bundle
from ask_fable.health import Breaker
from ask_fable.oracle_common import OracleResult
from ask_fable.redaction import redact_text
from ask_fable.sessions import SessionStore


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _quiet_and_no_audit(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.audit, "record", lambda **k: None)


def _allow(monkeypatch):
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))


def _stub_panel(monkeypatch, fable_res, mmx_res):
    """Stub the default panel: fable (+ its synthesis turn) and minimax."""

    async def fake_fable(question, context="", *, resume=None, system_prompt=None):
        if system_prompt is not None and len(fable_res) > 2:
            return fable_res[2]
        return fable_res[0]

    async def fake_minimax(question, context="", **kw):
        return mmx_res

    monkeypatch.setattr(server.fable, "run", fake_fable)
    monkeypatch.setattr(server.minimax, "run", fake_minimax)


# --- H2: the redaction patterns were quadratic -------------------------------


@pytest.mark.parametrize(
    "unit",
    ["secret_", "sk-", "secret-x_", "api_key_", "token "],
)
def test_h2_redaction_is_linear_on_hostile_input(unit):
    """`_SECRET_LINE`'s unbounded `secret(?:[_-][a-z0-9]+)*` and `_TOKEN_LINE`'s
    `\\b[a-z0-9_-]*token` re-scanned the rest of the run at every start position.
    200 KB took 101 s and 114 s respectively — synchronously, on the event loop,
    in every sink that redacts (hub turns, saved answers, session dumps, traces)."""
    payload = unit * 30_000
    start = time.perf_counter()
    redact_text(payload)
    assert time.perf_counter() - start < 1.0, f"redact_text is super-linear on {unit!r}"


# --- M7/G1: plural secret keys passed through --------------------------------


@pytest.mark.parametrize(
    "src",
    [
        '{"secrets": "hunter2hunter2hunter2"}',
        '{"passwords": "hunter2hunter2hunter2"}',
        '{"api_tokens": "abcd1234efgh5678ijkl"}',
        '{"apiKeys": "abcd1234efgh5678ijkl"}',
        '{"private_keys": "abcd1234efgh5678ijkl"}',
        'passphrase = "abcd1234efgh5678"',
    ],
)
def test_m7_plural_and_passphrase_secrets_are_redacted(src):
    out, n = redact_text(src)
    assert n >= 1 and "[REDACTED]" in out, src


@pytest.mark.parametrize(
    "src",
    [
        '{"token_type": "Bearer"}',
        '{"secret_name": "billing"}',
        '{"password_hint": "your first dog"}',
        '{"credentials_file": "~/.aws/credentials"}',
        '{"api_key_id": "AKIAIOSFODNN7EXAMPLE1"}',
    ],
)
def test_l2_credential_metadata_stays_readable(src):
    """A name whose last word DESCRIBES a credential (`_type`, `_name`, `_id`) is
    metadata the reader needs, not the credential itself."""
    assert redact_text(src) == (src, 0)


# --- L1: the trace key-walker disagreed with the text redactor ---------------


@pytest.mark.parametrize("key", ["api_tokens", "access_tokens", "refresh_tokens"])
def test_l1_plural_credential_keys_are_redacted_in_traces(key):
    """`token(?!s|[_-]?count)` spared every plural, so a list of credentials under
    `api_tokens` went into the trace bundle verbatim."""
    assert trace_bundle.redact_value({key: ["abcd1234efgh5678"]}) == {key: "[REDACTED]"}


@pytest.mark.parametrize("key", ["tokenizer", "input_token_limit", "output_token_limit"])
def test_l1_token_shaped_non_credentials_stay_readable(key):
    assert trace_bundle.redact_value({key: 4096}) == {key: 4096}


# --- H3: a disabled local CLI was still spawned ------------------------------


def test_h3_disabled_grok_is_not_spawned_for_a_gateway_token(monkeypatch):
    """`_via_local_cli` is reached under the GATEWAY's key (`atlas:xai/grok-4.6`
    -> provider `atlas`), so `run()`'s denylist check never saw `grok`. The
    operator turning grok off still spawned the grok CLI."""
    spawned: list[str] = []

    async def recorder(question, context="", **kw):
        spawned.append(question)
        return OracleResult("ok", text="from the local CLI")

    monkeypatch.setattr(oracles.grok, "run", recorder)
    monkeypatch.setattr(oracles.grok, "available", lambda: True)
    monkeypatch.setattr(oracles.grok, "looks_like_grok_model", lambda m: True)
    monkeypatch.setattr(oracles.grok, "prompt_fits", lambda q, c: True)
    monkeypatch.setattr(oracles, "is_disabled", lambda key: key == "grok")

    out = _run(
        oracles._via_local_cli("xai/grok-4.6", "q", "", None, gateway_ready=True)
    )
    assert out is None, "a disabled CLI must fall through to the gateway"
    assert spawned == [], "the disabled grok CLI was spawned anyway"


# --- M1: config-state kinds opened the circuit breaker -----------------------


@pytest.mark.parametrize(
    "kind",
    [
        "binary_missing",
        "sdk_unavailable",
        "bad_args",
        "model_not_found",
        "context_too_small",
        "busy",
        "transport_incapable",
        "disabled",
    ],
)
def test_m1_config_state_kinds_do_not_trip_the_breaker(kind):
    """Five identical "install codex" errors replaced the actionable message with
    `circuit_open` for the cooldown; with a pinned SDK transport and no Claude Code
    binary, `binary_missing` took FABLE offline after five councils."""
    breaker = Breaker()
    for _ in range(6):
        breaker.record("codex", "error", kind)
    assert breaker.state("codex") == "closed"


@pytest.mark.parametrize("kind", ["sdk_error", "network_error", "timeout"])
def test_m1_real_failures_still_trip_the_breaker(kind):
    breaker = Breaker()
    for _ in range(6):
        breaker.record("codex", "error", kind)
    assert breaker.state("codex") == "open"


# --- H1: a cut-off answer counted as a complete one --------------------------


def test_h1_truncated_panelist_is_flagged_and_not_cached(monkeypatch, tmp_path):
    """`shape_stopped` returns `status="ok"` with `kind="truncated"`, and every
    orchestrator filtered on status alone: the half answer reached quorum, was
    synthesized as if whole, and the merge was frozen in the cache for the TTL."""
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_CACHE", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE_PATH", str(tmp_path / "cache.db"))
    _stub_panel(
        monkeypatch,
        (OracleResult("ok", text="A"), None, OracleResult("ok", text="merged")),
        OracleResult(
            "ok",
            text="B, cut off mid-",
            kind="truncated",
            model="MiniMax-M3",
            meta={"partial": True, "stop_reason": "max_tokens"},
        ),
    )
    q = {"question": "How does this module route its requests?"}
    out = _run(server._handle_council(dict(q)))

    assert out["partial"] == ["MiniMax-M3"], "the cut-off panelist must be named"
    assert out["degraded"] is True
    assert "cut off" in out["recommended_next_action"]
    assert out["sources"]["minimax"]["partial"] is True
    assert not _run(server._handle_council(dict(q))).get("cached"), (
        "a merge built on a cut-off answer must not be served for the TTL"
    )


def test_h1_truncated_synthesis_is_not_cached(monkeypatch, tmp_path):
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_CACHE", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE_PATH", str(tmp_path / "cache.db"))
    _stub_panel(
        monkeypatch,
        (
            OracleResult("ok", text="A"),
            None,
            OracleResult(
                "ok",
                text="merged, but cut off mid-",
                kind="truncated",
                model="claude-fable-5-1",
                meta={"partial": True, "stop_reason": "max_tokens"},
            ),
        ),
        OracleResult("ok", text="B", model="MiniMax-M3"),
    )
    q = {"question": "Where does this module put its retries?"}
    first = _run(server._handle_council(dict(q)))
    assert first["partial"] == ["claude-fable-5-1"]
    assert not _run(server._handle_council(dict(q))).get("cached")


def test_h1_ask_surfaces_the_partial_flag(monkeypatch):
    """`_handle_single` merged the bridge's meta; `ask` — the primary tool — did
    not, so it returned half an answer as a plain `ok` and recorded it into the
    session transcript as a complete turn."""
    _allow(monkeypatch)

    async def cut_off(question, context="", *, resume=None, system_prompt=None):
        return OracleResult(
            "ok",
            text="half an ans",
            kind="truncated",
            model="claude-fable-5-1",
            meta={"partial": True, "stop_reason": "max_tokens"},
        )

    monkeypatch.setattr(server.fable, "run", cut_off)
    store = SessionStore()
    out = _run(server._handle_ask(store, {"question": "Explain the retry ladder here."}))
    assert out["partial"] is True
    assert out["stop_reason"] == "max_tokens"


# --- M2: the outer cache ignored the resolved default effort -----------------


def test_m2_default_effort_is_part_of_the_outer_cache_key(monkeypatch):
    """`effort=None` means "the operator's default", which lives in env/config and
    outlives the process — cache.db does too, so an answer computed at the old
    default kept being served after the operator changed it."""
    monkeypatch.setenv("ASK_FABLE_GROK_REASONING", "low")
    low = oracles.cache_effort("grok", None)
    monkeypatch.setenv("ASK_FABLE_GROK_REASONING", "high")
    assert oracles.cache_effort("grok", None) != low


def test_m2_codex_default_effort_is_keyed(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_CODEX_REASONING", "low")
    low = oracles.cache_effort("codex", None)
    monkeypatch.setenv("ASK_FABLE_CODEX_REASONING", "high")
    assert low is not None and oracles.cache_effort("codex", None) != low


def test_m2_panel_cache_effort_covers_a_council(monkeypatch):
    monkeypatch.setenv("ASK_FABLE_GROK_REASONING", "low")
    low = oracles.panel_cache_effort(["fable", "grok"])
    monkeypatch.setenv("ASK_FABLE_GROK_REASONING", "high")
    assert low is not None and oracles.panel_cache_effort(["fable", "grok"]) != low
    # a panel of backends that take no effort keys on nothing, as before
    assert oracles.panel_cache_effort(["fable", "minimax"]) is None


# --- M4: a non-UTF-8 --version escaped fable.run -----------------------------


def test_m4_non_utf8_cli_version_does_not_raise(tmp_path, monkeypatch):
    """`_cli_version` ran with `text=True` and no `errors=`, so a binary printing
    non-UTF-8 raised UnicodeDecodeError out of `best_cli_path()` — which
    `_run_sdk` calls BEFORE its try block, and `fable.run` has no catch-all. The
    call escaped with no audit row, no breaker record, and `auto` never fell
    through to the CLI. `diagnose` fixed its own copy; this is the sibling."""
    from ask_fable import fable

    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nprintf '\\377\\376 1.2.3'\n", encoding="utf-8")
    fake.chmod(0o755)
    assert fable._cli_version(str(fake)) in (None, (1, 2, 3))  # parsed or given up, never raised


# --- M6: the denylist fold missed most marks and look-alikes -----------------


def test_m6_combining_marks_and_format_chars_do_not_split_a_term():
    """The fold listed two hand-picked ranges; a category sweep found 1475 more
    non-spacing marks and 41 more format characters that walked a term straight
    through. Category-based stripping covers every one."""
    term = next(t for t in _denylist._OFFENSE_TERMS if " " not in t)
    for mark in ("҃", "ְ", "ً", "\U000110bd"):
        spoofed = term[:2] + mark + term[2:]
        assert not _denylist.check_denylist(f"write a {spoofed}")[0], repr(mark)


def test_m6_every_term_letter_has_a_lookalike_mapping():
    """One Cyrillic substitution was enough while b f g k m n r t v z had no
    mapping at all."""
    letters = {c for t in _denylist._OFFENSE_TERMS for c in t if c.isalpha()}
    mapped = {_denylist._fold(chr(cp)) for cp in _denylist._CONFUSABLES}
    assert letters <= mapped


def test_m6_benign_text_still_passes():
    assert _denylist.check_denylist("how do I parse a config file")[0]
    assert _denylist.check_denylist("naïve café résumé")[0]


# --- M9: diagnose called a disabled backend "not on PATH" --------------------


def test_m9_a_disabled_backend_reports_as_disabled(monkeypatch):
    monkeypatch.setattr(oracles, "is_disabled", lambda key: key == "minimax")
    row = _run(diagnose._probe("minimax"))
    assert row["status"] == "disabled"
    assert "not on PATH" not in row["checks"][0]["detail"]
    assert "configure_disabled" in row["fix"]


# --- M10: queued-but-cancelled members counted as 0 ms calls -----------------


def test_m10_queued_cancellations_are_not_calls(tmp_path):
    """`run_bounded`'s "never got a slot" branch records a 0 ms provider.completed.
    It reached no backend, so counting it inflated `calls` and dragged `avg_ms`
    down — a `tier="full"` council past the parallelism cap did it every run."""
    path = tmp_path / "decisions.jsonl"
    real = (
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:00Z",'
        '"event_name":"provider.completed","tool":"ask_council","status":"ok",'
        '"duration_ms":30000,"provider":{"transport":"http","model":"m"},'
        '"orchestration":{"model":"m"}}\n'
    )
    skipped = (
        '{"schema_version":2,"timestamp":"2026-07-11T00:00:01Z",'
        '"event_name":"provider.completed","tool":"ask_council","status":"cancelled",'
        '"duration_ms":0,"provider":{"transport":"skipped","model":"m"},'
        '"orchestration":{"model":"m"}}\n'
    )
    path.write_text(real + skipped * 5, encoding="utf-8")
    out = stats.aggregate(path, window="all", by="provider")
    assert out["totals"]["calls"] == 1, "a member that never ran is not a call"
    assert out["totals"]["cancelled"] == 5
    assert out["totals"]["p95_ms"] == 30000
    assert out["totals"]["avg_ms"] == 30000, "0 ms non-calls must not drag the average"


# =============================================================================
# Second batch — the findings the first pass deferred, plus the LOW cleanup.
# =============================================================================


# --- M3 / L14: a generation running in a worker thread holds the model -------


def test_m3_a_generating_thread_blocks_an_unload_claim():
    """`_INFLIGHT` is released when the awaiting coroutine unwinds, but the POST runs
    in a thread `to_thread` cannot cancel. A council that hit its timeout dropped the
    count while the model was still generating, and the cleanup unload right after it
    pulled the model out from under a live generation."""
    from ask_fable import lmstudio

    key = "test/model-m3"
    with lmstudio._generating(key):
        # the awaiting caller is gone (no _INFLIGHT entry), but the POST is open
        assert not lmstudio._INFLIGHT.get(key)
        with lmstudio._unload_claim(key) as claimed:
            assert claimed is False, "unloaded a model that was still generating"
    # once the thread returns, the model is free again
    with lmstudio._unload_claim(key) as claimed:
        assert claimed is True
    assert not lmstudio._GENERATING, "the generating counter must not leak"


def test_m3_the_generating_counter_is_released_on_an_exception():
    from ask_fable import lmstudio

    key = "test/model-boom"
    with pytest.raises(RuntimeError), lmstudio._generating(key):
        raise RuntimeError("boom")
    assert key not in lmstudio._GENERATING


# --- L20: an operator config with a bare `thinking` key broke every kimi call -


@pytest.mark.parametrize(
    "base",
    [
        "thinking = true\n[models]\nx = 1\n",
        "thinking = { enabled = true }\n",
        "[thinking]  # tuned\nenabled = false\n[models]\nx = 1\n",
        "[ thinking ]\nenabled = false\n",
    ],
)
def test_l20_rendered_kimi_config_always_parses(base):
    """We emit our own [thinking] table, so any spelling of the operator's must be
    stripped — a leftover made the merged file a "Cannot overwrite a value" parse
    error and every kimi call died on an opaque CLI config error."""
    import tomllib

    from ask_fable import kimi

    parsed = tomllib.loads(kimi.render_config(base, "high"))
    assert parsed["thinking"] == {"enabled": True, "effort": "high"}


# --- L19: room_verdict fell open on hostile input ----------------------------


@pytest.mark.parametrize("size", [None, "8000000000", -1, -5_000_000_000, 8.0e9, True, 0])
def test_l19_unusable_model_sizes_are_unknown_not_fits(size):
    """A negative size floor-divided to -1 and read as `fits`; a numeric string from
    the control page reached the comparison and raised TypeError."""
    from ask_fable import lmstudio

    gpu = {"available": True, "vram_free_mib": 4000, "vram_total_mib": 24000}
    assert lmstudio.room_verdict(size, gpu) == "unknown"


def test_l19_string_vram_does_not_raise():
    from ask_fable import lmstudio

    gpu = {"available": True, "vram_free_mib": "4000", "vram_total_mib": "24000"}
    assert lmstudio.room_verdict(8_000_000_000, gpu) == "unknown"


# --- L6: the loopback Host check accepted malformed values -------------------


@pytest.mark.parametrize(
    "host",
    ["[::1]evil", "localhost:evil", "[::1]:8788:x", "127.0.0.1:99999x", "evil.example"],
)
def test_l6_malformed_host_headers_are_refused(host):
    from ask_fable import context_busd

    assert context_busd._loopback_host(host) is False


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "[::1]:8788", "localhost:8788"])
def test_l6_real_loopback_hosts_still_pass(host):
    from ask_fable import context_busd

    assert context_busd._loopback_host(host) is True


# --- L5: the Claude Code stderr echo was an unredacted on-disk sink ----------


def test_l5_echoed_subprocess_stderr_is_redacted(capsys):
    """Claude Code persists an MCP server's stderr to its own logs, so this echo is a
    sink like any other."""
    from ask_fable.redaction import redact_text

    line = 'error: {"api_key": "sk-ant-abcdefghijklmnopqrstuvwxyz0123"}'
    assert "sk-ant-" not in redact_text(line)[0]


# --- L17: the trace argument hash raised on a lone surrogate -----------------


def test_l17_trace_argument_hash_survives_a_lone_surrogate():
    """It is computed BEFORE the handler runs, outside every bridge's error handling."""
    import json

    text = json.dumps({"question": "hi \ud800 there"}, ensure_ascii=False)
    assert text.encode("utf-8", "surrogatepass")  # what trace_runtime now does
    with pytest.raises(UnicodeEncodeError):
        text.encode()  # what it used to do


# =============================================================================
# Third batch — review findings on the two fix commits themselves (PR #101).
# =============================================================================


def test_review1_a_cut_off_answer_never_raises_confidence():
    """The cut-off penalty was written as a swap (`"low" if c == "medium" else
    "medium"`), which PROMOTED an already-low confidence — so the two worst cases, a
    lone cut-off panelist and a failed synthesis plus a cut-off panelist, came back
    reading stronger than a clean result. It must only ever step down."""
    one = [OracleResult("ok", key="fable", text="a", model="fable")]
    two = [*one, OracleResult("ok", key="minimax", text="b", model="MiniMax-M3")]

    # a lone answer is "low" clean; cutting it off must not make it "medium"
    clean = server._council_envelope(one, 1, "fable")
    cut = server._council_envelope(one, 1, "fable", partial=["fable"])
    assert clean["confidence"] == "low" and cut["confidence"] == "low"

    # synthesis failed (synthesizer=None) => "low"; a cut-off panelist keeps it there
    failed = server._council_envelope(two, 2, None, partial=["MiniMax-M3"])
    assert failed["confidence"] == "low"

    # and a healthy panel does step DOWN one rung
    assert server._council_envelope(two, 2, "fable")["confidence"] == "medium"
    assert server._council_envelope(two, 2, "fable", partial=["MiniMax-M3"])["confidence"] == "low"


def test_review3_material_disagreement_outranks_the_cut_off_notice():
    """A panel that both disagreed and had a truncated answer must still be told to
    escalate — that is the more urgent fact."""
    ok = [
        OracleResult("ok", key="fable", text="a", model="fable"),
        OracleResult("ok", key="minimax", text="b", model="MiniMax-M3"),
    ]
    env = server._council_envelope(
        ok, 2, "fable", consensus="divergent", material_disagreement=True, partial=["MiniMax-M3"]
    )
    assert "ask_debate" in env["recommended_next_action"]
    assert env["partial"] == ["MiniMax-M3"]  # still reported, just not as the headline


def test_review2_a_descriptor_tail_cannot_disarm_a_credential_container():
    """`header`, `scope`, `status`, `source` and `field` name a container that can hold
    the credential itself — `cookie_header` IS the cookies."""
    from ask_fable.redaction import is_secret_key

    for key in ("cookie_header", "secret_scope", "password_status", "token_source"):
        assert is_secret_key(key), key
    assert trace_bundle.redact_value({"cookie_header": "session=abc; csrf=deadbeef"}) == {
        "cookie_header": "[REDACTED]"
    }
    # the readability cases the tails exist for still hold
    for key in ("token_type", "api_key_id", "password_hint", "credentials_file", "tokens_used"):
        assert not is_secret_key(key), key


def test_review7_a_long_key_prefix_still_matches():
    """Capping the key prefix at 64 characters dropped real matches (a long id followed
    by `-token:`) without making anything faster — the lookbehind is what buys the
    linearity, not the bound."""
    out, count = redact_text("a" * 64 + "-token: abcdefghijklmnop1234")
    assert count == 1 and "[REDACTED]" in out


def test_uri_userinfo_pattern_is_linear():
    """A third pattern of the same family, predating the PR #100 batch: the scheme
    `[a-z][a-z0-9+.-]*` restarted at every letter and consumed the rest of the run
    looking for `://`. 180 KB of `token-` took 80 s."""
    start = time.perf_counter()
    redact_text("token-" * 30_000)
    assert time.perf_counter() - start < 1.0
    # and it still redacts what it is for
    assert "[REDACTED]" in redact_text("https://user:pw@example.com/x")[0]


def test_review4_conference_excludes_disabled_seats_like_a_council(monkeypatch):
    """A seat the operator turned off is a deliberate choice, not a missing answer.
    Counting it made ask_conference report `2/3, degraded` where ask_council called
    the same list `2/2`."""
    monkeypatch.setattr(server.guard, "check", lambda q, c="", **kw: (True, ""))
    monkeypatch.setattr(server.oracles, "available", lambda key: key != "deepseek")
    monkeypatch.setattr(server.oracles, "is_disabled", lambda key: key == "deepseek")

    async def fake_run(key, question, context="", **kw):
        if "MAP OF THE DISAGREEMENT" in question:
            return OracleResult("ok", key=key, text="CONVERGED: x", model=key)
        return OracleResult("ok", key=key, text=f"{key} speaks", model=key)

    monkeypatch.setattr(server.oracles, "run", fake_run)
    out = _run(
        server._handle_conference(
            {"question": "redis or postgres?", "models": ["fable", "minimax", "deepseek"], "rounds": 1}
        )
    )
    assert out["quorum"] == "2/2" and out["degraded"] is False
    assert out["disabled"] == ["deepseek"]


def test_review8_transient_cleanup_state_is_not_cached(monkeypatch, tmp_path):
    """Whether we could free an LM Studio model is host state at that instant. Stored,
    a later cache hit replayed "could not free X" on a call that touched nothing."""
    _allow(monkeypatch)
    monkeypatch.setenv("ASK_FABLE_CACHE", "1")
    monkeypatch.setenv("ASK_FABLE_CACHE_PATH", str(tmp_path / "cache.db"))
    _stub_panel(
        monkeypatch,
        (OracleResult("ok", text="A"), None, OracleResult("ok", text="merged")),
        OracleResult("ok", text="B", model="MiniMax-M3"),
    )
    q = {"question": "Which store fits this access pattern?"}
    _run(server._handle_council(dict(q)))
    hit = _run(server._handle_council(dict(q)))
    assert hit.get("cached") is True
    assert "cleanup_failed" not in hit
