"""Tests for the structured-sidecar parser (sidecar.extract / decorate).

Cases mirror what real backends emitted during scripts/probe_sidecar.py — notably
Gemini's decoy ```python blocks before the real ```json-sidecar block."""

from __future__ import annotations

from ask_fable import sidecar


def _block(rec="apply", conf="high", nc="[]"):
    return f'```json-sidecar\n{{"sidecar_version": 1, "recommendation": "{rec}", "confidence": "{conf}", "needs_context": {nc}}}\n```'


def test_clean_extract_and_strip():
    text = f"Use lru_cache.\n\n{_block()}"
    prose, sc = sidecar.extract(text)
    assert prose == "Use lru_cache."
    assert sc == {"recommendation": "apply", "confidence": "high", "needs_context": []}


def test_ignores_decoy_python_fences():
    # The Gemini shape: prose + two ```python content blocks + the real sidecar last.
    text = (
        "Use lru_cache.\n\n```python\ndef f(): ...\n```\n\nReplacement:\n\n"
        "```python\n@lru_cache\ndef f(): ...\n```\n\n" + _block()
    )
    prose, sc = sidecar.extract(text)
    assert sc is not None and sc["recommendation"] == "apply"
    assert "```python" in prose  # the code blocks stay in the prose
    assert "json-sidecar" not in prose  # only the sidecar is stripped


def test_missing_block_is_none_not_error():
    prose, sc = sidecar.extract("Just prose, no block here.")
    assert sc is None and prose == "Just prose, no block here."


def test_incidental_json_block_without_sentinel_ignored():
    text = 'Here is a config:\n\n```json\n{"port": 8080}\n```'
    prose, sc = sidecar.extract(text)
    assert sc is None and prose == text


def test_repairs_trailing_comma_and_smart_quotes():
    text = 'Ans.\n\n```json-sidecar\n{“sidecar_version”: 1, “recommendation”: “reject”, “confidence”: “low”, “needs_context”: [],}\n```'
    prose, sc = sidecar.extract(text)
    assert sc is not None and sc["recommendation"] == "reject" and sc["confidence"] == "low"


def test_unknown_enum_values_dropped():
    text = f'Ans.\n\n{_block(conf="pretty sure")}'
    _, sc = sidecar.extract(text)
    assert sc is not None and sc["confidence"] is None  # invalid enum → None, not garbage


def test_sentinel_without_recognized_fence_still_found():
    text = 'Ans.\n\n```json\n{"sidecar_version": 1, "recommendation": "investigate", "confidence": "medium", "needs_context": ["auth.py"]}\n```'
    _, sc = sidecar.extract(text)
    assert sc is not None and sc["recommendation"] == "investigate" and sc["needs_context"] == ["auth.py"]


def test_multiple_sidecar_blocks_fail_safe_and_strip_all():
    # A decoy/example json-sidecar block after the real one must NOT surface a wrong
    # recommendation, and neither block may leak into the prose.
    text = f'Real answer.\n\n{_block(rec="apply")}\n\nFor reference:\n\n{_block(rec="reject")}'
    prose, sc = sidecar.extract(text)
    assert sc is None  # non-compliant (2 blocks) → fail safe, don't trust either
    assert "json-sidecar" not in prose and "reject" not in prose and "apply" not in prose


def test_invalid_recommendation_block_is_stripped_no_leak():
    # Recognizable json-sidecar fence but off-enum recommendation: strip it (no raw
    # JSON in the prose), and don't surface the bad value.
    text = 'Answer.\n\n```json-sidecar\n{"sidecar_version": 1, "recommendation": "apply.", "confidence": "high", "needs_context": []}\n```'
    prose, sc = sidecar.extract(text)
    assert "json-sidecar" not in prose and "apply." not in prose  # block stripped
    assert sc is not None and sc["recommendation"] is None  # bad enum → None, not leaked


def test_malformed_json_sidecar_fence_is_stripped():
    # A json-sidecar fence whose body is broken JSON: still clearly the sidecar, so
    # strip it; value unavailable → None.
    text = 'Answer.\n\n```json-sidecar\n{"sidecar_version": 1, "recommendation": "apply"  BROKEN\n```'
    prose, sc = sidecar.extract(text)
    assert "json-sidecar" not in prose and "BROKEN" not in prose  # stripped, no leak
    assert sc is None


def test_decorate_hit():
    payload = {"status": "ok", "model": "m", "answer": "raw"}
    out = sidecar.decorate(payload, f"Real answer.\n\n{_block()}")
    assert out["answer"] == "Real answer."
    assert out["missing_sidecar"] is False
    assert out["sidecar"]["recommendation"] == "apply"


def test_decorate_miss_is_non_breaking():
    payload = {"status": "ok", "model": "m", "answer": "raw"}
    out = sidecar.decorate(payload, "Plain answer, no block.")
    assert out["answer"] == "Plain answer, no block."
    assert out["sidecar"] is None and out["missing_sidecar"] is True
