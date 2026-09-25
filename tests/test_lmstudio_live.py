"""Opt-in contract tests against a REAL LM Studio server — the sacrificial box.

These are deliberately NOT part of the hermetic suite. ``test_lmstudio.py``
monkeypatches ``lms._request`` with an in-process fake, so it can only assert
"if the server answers X, we do Y" — and the fake encodes our BELIEFS about
LM Studio (the memory-failure vocabulary ``_MEMORY_HINTS`` matches, the load/
context behaviour, the unload confirmation). This file is where those beliefs
meet llama.cpp. A 4B model is a bad oracle and a fine contract target.

It is also the rehearsal target for anything destructive — real loads, huge KV
allocations, real unloads. Never point it at the primary lmstudio.example.com server; that
box is answering real calls. The whole suite is excluded by default via
``addopts``, so a stray env var can never make CI touch a live box.

Run it explicitly, with the gates set:

    ASK_FABLE_LMS_LIVE=http://lmstudio-host:1234 \
    ASK_FABLE_LMS_LIVE_MODEL=nvidia/nemotron-3-nano-4b \
    pytest tests/test_lmstudio_live.py -m lms_live -v

``ASK_FABLE_LMS_LIVE`` must hold the base URL of the SACRIFICIAL box (env only,
never config); a known primary host is refused unless
``ASK_FABLE_LMS_LIVE_UNSAFE=1`` is set too.

Safety rails:
- short timeouts and a small output cap, so a run cannot hang for minutes;
- a model the suite itself loaded is unloaded on teardown; a pre-suite resident
  that LM Studio evicts to fit a load is reloaded at its previous context
  (LM Studio's own eviction is the one thing the suite cannot prevent);
- the room check is pointed at a dead control page on purpose (see _live_env).
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import string

import pytest

import ask_fable.lmstudio as lms

LIVE_URL = (os.environ.get("ASK_FABLE_LMS_LIVE") or "").strip().rstrip("/")
MODEL_OVERRIDE = (os.environ.get("ASK_FABLE_LMS_LIVE_MODEL") or "").strip()
PRIMARY_HOSTS = {"lmstudio.example.com", "192.0.2.10"}

LIVE_TIMEOUT = 240.0  # a real 8 GB Thunderbolt eGPU is slow; still bounded
LIVE_MAX_TOKENS = 768  # answers are tiny on purpose; must stay >= _MIN_OUTPUT_TOKENS
LIVE_CONTEXT = 8192  # the context the suite explicitly loads at

# Who was resident BEFORE the suite touched anything, and at what context.
# Captured by _live_env (autouse, module-scoped => before the first test) so
# teardown can restore the box as found. LM Studio may itself evict a resident
# to fit a big load — not something the suite can prevent, only repair.
_RESIDENT_BEFORE: dict[str, int] = {}

pytestmark = pytest.mark.lms_live


def _live_host() -> str:
    return re.sub(r"^https?://", "", LIVE_URL).split(":")[0].lower()


def _usable() -> bool:
    if not LIVE_URL:
        return False
    if _live_host() in PRIMARY_HOSTS and os.environ.get("ASK_FABLE_LMS_LIVE_UNSAFE") != "1":
        return False
    return True


def _chat_models(cat: dict) -> list[dict]:
    return [m for m in cat["models"] if m["type"] in ("llm", "vlm")]


def _pick_model(cat: dict) -> str | None:
    """The model under test: ASK_FABLE_LMS_LIVE_MODEL, else the smallest chat
    model (safest thing to load on a small card)."""
    if MODEL_OVERRIDE:
        return MODEL_OVERRIDE
    candidates = sorted(_chat_models(cat), key=lambda m: m["size_bytes"] or 1 << 60)
    return candidates[0]["key"] if candidates else None


def _run(model: str, question: str, context: str = ""):
    return asyncio.run(lms.run(model, question, context))


@pytest.fixture(scope="module", autouse=True)
def _live_env():
    """Gate + environment for the whole module. Skips (not fails) when unset."""
    if not _usable():
        pytest.skip(
            "live LM Studio suite: set ASK_FABLE_LMS_LIVE=<sacrificial base url> "
            "(see module docstring)"
        )
    cat = lms.catalog()
    assert cat["ok"], f"sacrificial box unreachable at {LIVE_URL}: {cat['error']}"
    for m in _chat_models(cat):
        if m["loaded"]:
            _RESIDENT_BEFORE[m["key"]] = m["loaded_context_length"] or LIVE_CONTEXT

    mp = pytest.MonkeyPatch()
    mp.setenv("ASK_FABLE_LMSTUDIO_BASE_URL", LIVE_URL)
    mp.setenv("ASK_FABLE_LMSTUDIO_TIMEOUT", str(LIVE_TIMEOUT))
    mp.setenv("ASK_FABLE_LMSTUDIO_LOAD_TIMEOUT", str(LIVE_TIMEOUT))
    mp.setenv("ASK_FABLE_LMSTUDIO_UNLOAD_WAIT", "60")
    mp.setenv("ASK_FABLE_LMSTUDIO_MAX_TOKENS", str(LIVE_MAX_TOKENS))
    mp.setenv("ASK_FABLE_LMSTUDIO_CONTEXT", str(LIVE_CONTEXT))
    # Ask-first: this suite never evicts a resident model to make room.
    mp.setenv("ASK_FABLE_LMSTUDIO_SWAP", "never")
    # No control page on the sacrificial box: the host-match guard already
    # degrades the room check to "unknown" (controlpage.monitors), and the dead
    # URL makes that explicit and DNS-independent.
    mp.setenv("ASK_FABLE_CONTROL_URL", "http://127.0.0.1:9")
    yield
    # Restore the box to its pre-suite state, in both directions:
    #   - anything resident now that was not before: the suite loaded it (e.g.
    #     the recovery test's smaller-context retry); free it again.
    #   - anything resident before that vanished: LM Studio evicted it by
    #     ITSELF to fit a big load (observed: nemotron dropped for a 133k
    #     window); reload it at its previous context.
    after = lms.catalog()
    if after.get("ok"):
        resident_now = {m["key"] for m in _chat_models(after) if m["loaded"]}
        for key in sorted(resident_now - set(_RESIDENT_BEFORE)):
            out = lms.unload(key)
            print(f"\n[live suite] unloading {key} (loaded during the run): {out.get('status')}")
        for key, ctx in _RESIDENT_BEFORE.items():
            if key in resident_now:
                continue
            _info, lerr, _st = lms._load_instance(key, ctx, 180.0)
            print(
                f"\n[live suite] pre-suite resident {key} had been evicted; "
                f"reload at {ctx}: {'ok' if lerr is None else lerr}"
            )
    mp.undo()


@pytest.fixture(scope="module")
def live_model(_live_env) -> str:
    cat = lms.catalog()
    picked = _pick_model(cat)
    assert picked, (
        "no chat model on the sacrificial box — load one, or set ASK_FABLE_LMS_LIVE_MODEL"
    )
    keys = {m["key"] for m in cat["models"]}
    assert picked in keys, f"{picked!r} is not on the box; have: {sorted(keys)[:12]}"
    return picked


def test_live_catalog_reports_loaded_context(_live_env):
    cat = lms.catalog()
    assert cat["ok"], cat["error"]
    assert cat["models"], "the box reports no models at all"
    for m in cat["models"]:
        if m["loaded"]:
            assert m["instance_id"], f"{m['key']} is loaded without an instance id"


def test_live_chat_shapes_a_real_completion(live_model):
    res = _run(live_model, "Reply with exactly the word PONG and nothing else.")
    assert res.status == "ok", res.text
    assert "PONG" in res.text.upper()
    assert res.meta.get("model_loaded") is True
    assert res.telemetry is not None and res.telemetry.usage is not None, (
        "a real completion must report usage (the OpenAI shape we parse)"
    )
    assert (res.telemetry.usage.input_tokens or 0) > 0


def test_live_buried_nonce_survives_the_loaded_context(live_model):
    """If LM Studio silently shrinks the prompt to fit (the failure class
    lmstudio.py:17-22 calls the worst), a nonce buried mid-prompt vanishes and
    the model cannot return it. Ground truth for the ``_truncated`` probe — a
    fake cannot produce a real quiet truncation."""
    cat = lms.catalog()
    max_ctx = next(m for m in cat["models"] if m["key"] == live_model)["max_context_length"] or 0
    if max_ctx and max_ctx < LIVE_CONTEXT:
        pytest.skip(f"{live_model} max context {max_ctx} < {LIVE_CONTEXT}")

    nonce = "ZK" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    # ~50% of the window at a conservative 3.5 chars/token, nonce in the middle.
    budget_chars = int(LIVE_CONTEXT * 0.5 * 3.5)
    base = "the quick brown fox jumps over the lazy dog. "
    filler = (base * (budget_chars // len(base) + 1))[:budget_chars]
    half = len(filler) // 2
    prompt = (
        filler[:half]
        + f"\nThe secret codeword is {nonce}.\n"
        + filler[half:]
        + "\nQuestion: what is the secret codeword? Reply with only the codeword."
    )
    res = _run(live_model, prompt)
    assert res.status == "ok", res.text
    assert nonce in res.text.upper(), (
        f"codeword {nonce} lost — the prompt was truncated, or {live_model} is too weak "
        "to copy a short token; pin a stronger ASK_FABLE_LMS_LIVE_MODEL"
    )
    # The probe's own verdict must agree with ground truth.
    assert res.kind != "truncated", res.text


def test_live_oversized_window_recovers_at_the_needed_context(_live_env):
    """An absurd window (196k) with a SMALL prompt: the first load crashes the
    engine (KV for that window cannot fit the card), and the non-destructive
    smaller-context retry must recover at the prompt's real size, reporting
    ``context_reduced``.

    OPT-IN because it forces one real engine crash (a coredump): set
    ``ASK_FABLE_LMS_LIVE_CRASH_PROBE=1``. It also bypasses the host's configured
    ``lmstudio_context_ceilings`` on purpose — normal calls on a capped host can
    no longer even request this window; the point here is that IF one slips
    through (an uncapped host, a raised ceiling), the recovery still works."""
    if os.environ.get("ASK_FABLE_LMS_LIVE_CRASH_PROBE") != "1":
        pytest.skip("set ASK_FABLE_LMS_LIVE_CRASH_PROBE=1 (one engine crash, ~1-2 min)")
    cat = lms.catalog()
    size = lambda m: m["size_bytes"] or 1 << 60  # noqa: E731
    probe = next((m for m in sorted(_chat_models(cat), key=size) if not m["loaded"]), None)
    if probe is None:
        pytest.skip("every chat model is currently resident")
    max_ctx = probe["max_context_length"] or 0
    want = min(max_ctx, 196608) if max_ctx else 196608
    if want < 131072:
        pytest.skip(f"{probe['key']} max context {max_ctx} is too small to force a crash")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ASK_FABLE_LMSTUDIO_CONTEXT", str(want))
        mp.setattr(lms, "context_ceiling", lambda: 0)  # deliberate bypass, see docstring
        res = _run(probe["key"], "Reply with exactly OK and nothing else.")
    if res.status == "ok" and "context_reduced" not in res.meta:
        pytest.fail(
            f"the {want}-token load of {probe['key']} succeeded outright — the crash repro "
            "no longer applies (nothing to recover from)"
        )
    assert res.status == "ok", f"the smaller-context recovery failed: {res.text[:400]}"
    reduced = res.meta["context_reduced"]
    assert reduced["from"] == want and reduced["to"] < 32768, reduced
    assert "OK" in res.text.upper()


def test_live_crash_probe_proves_memory_but_is_opt_in(_live_env):
    """The full three-tier path: crash -> smaller retry (crashes too) -> floor
    probe LOADS => memory-bound => ask-first offer, with only our own probe
    freed (resident models untouched).

    OPT-IN because it forces two consecutive engine crashes plus a probe
    (~2-3 min and two coredumps): set ``ASK_FABLE_LMS_LIVE_CRASH_PROBE=1``.
    The same path is covered hermetically in test_lmstudio.py; this is the
    live falsification of the probe's core assumption — a readable file loads
    at floor context even under VRAM pressure (weights spill to CPU)."""
    if os.environ.get("ASK_FABLE_LMS_LIVE_CRASH_PROBE") != "1":
        pytest.skip("set ASK_FABLE_LMS_LIVE_CRASH_PROBE=1 (two engine crashes, ~2-3 min)")
    cat = lms.catalog()
    size = lambda m: m["size_bytes"] or 1 << 60  # noqa: E731
    probe = next((m for m in sorted(_chat_models(cat), key=size) if not m["loaded"]), None)
    if probe is None:
        pytest.skip("every chat model is currently resident")
    max_ctx = probe["max_context_length"] or 0
    want = min(max_ctx, 196608) if max_ctx else 196608
    if want < 131072:
        pytest.skip(f"{probe['key']} max context {max_ctx} is too small to force a crash")
    # ~131k estimated tokens of filler => `needed` sits in the crash zone too,
    # so the smaller-context retry cannot rescue it and the probe must run.
    filler = ("the quick brown fox jumps over the lazy dog. " * 13000)[:460000]

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ASK_FABLE_LMSTUDIO_CONTEXT", str(want))
        mp.setattr(lms, "context_ceiling", lambda: 0)  # deliberate bypass, see above
        res = _run(probe["key"], "Reply with OK.", context=filler)
    assert res.status == "error", f"expected a proven memory shortfall, got: {res.text[:300]}"
    assert res.meta.get("crash_probe") == "loaded_at_floor", (
        "the floor probe did not prove a memory-bound crash here; evidence "
        f"{res.meta.get('crash_probe')!r}: {res.text[:300]}"
    )
    assert res.meta.get("unload_offer"), "a proven memory shortfall with a resident must offer"
    assert "memory-bound" in res.text
    still = {m["key"] for m in _chat_models(lms.catalog()) if m["loaded"]}
    assert set(_RESIDENT_BEFORE) <= still, (
        f"a pre-suite resident was unloaded: {sorted(set(_RESIDENT_BEFORE) - still)}"
    )


def test_live_transport_failure_is_loud_and_clean(_live_env):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ASK_FABLE_LMSTUDIO_BASE_URL", "http://127.0.0.1:9")
        mp.setenv("ASK_FABLE_LMSTUDIO_TIMEOUT", "5")
        mp.setenv("ASK_FABLE_LMSTUDIO_LOAD_TIMEOUT", "5")
        res = _run("any/model", "hello")
    assert res.status == "error"
    assert "unreachable" in res.text


def test_live_unload_is_confirmed_when_the_suite_loaded_the_model(live_model):
    if live_model in _RESIDENT_BEFORE:
        pytest.skip("model was resident before the suite — leaving the user's load alone")
    _run(live_model, "ok")  # ensure loaded, by us
    out = lms.unload(live_model)
    assert out.get("status") == "ok", out
    assert out.get("unload_confirmed") is True, out
    cat = lms.catalog()
    entry = next(m for m in cat["models"] if m["key"] == live_model)
    assert entry["loaded"] is False
