"""The declared ask_council models schema must accept everything
oracles.resolve() accepts — a narrower schema makes strictly-validating MCP
hosts reject documented calls (atlas:/alias tokens) client-side."""

from __future__ import annotations

import re

import ask_fable.server as server
from ask_fable import oracles


def _matches(spec: dict, token: str) -> bool:
    for branch in spec["anyOf"]:
        if "enum" in branch and token in branch["enum"]:
            return True
        if "pattern" in branch and re.match(branch["pattern"], token):
            return True
    return False


def _accepts(token: str) -> bool:
    return _matches(server._COUNCIL_SCHEMA["properties"]["models"]["items"], token)


def test_schema_accepts_known_and_aliases():
    for token in list(oracles.KNOWN) + sorted(oracles.ALIASES):
        assert _accepts(token), token


def test_schema_accepts_dynamic_tokens():
    # The exact usages documented in CLAUDE.md / README.
    assert _accepts("ollama:kimi-k2.7-code:cloud")
    assert _accepts("atlas:zai-org/glm-5.2")
    assert _accepts("atlas:deepseek-ai/deepseek-v4-pro")
    assert _accepts("ali:qwen3.8-max")  # Alibaba/Qwen gateway tokens
    assert _accepts("lmstudio:qwen3.8-27b")


def test_conference_schema_matches_resolve_grammar():
    # Same invariant on the conference bench: every gateway family resolve()
    # accepts must be accepted client-side too.
    items = server._CONFERENCE_SCHEMA["properties"]["models"]["items"]
    for token in list(oracles.KNOWN) + sorted(oracles.ALIASES):
        assert _matches(items, token), token
    for token in ("ali:qwen3.8-max", "atlas:zai-org/glm-5.2", "lmstudio:x", "ollama:y"):
        assert _matches(items, token), token
    assert not _matches(items, "gpt-4") and not _matches(items, "ali:")


def test_schema_still_rejects_garbage():
    assert not _accepts("gpt-4")
    assert not _accepts("atlas:")
    assert not _accepts("ollama:")


def test_synthesizer_property_accepts_council_grammar():
    # `synthesizer` must accept exactly what a member token accepts, on both
    # council schemas — a narrower grammar would reject documented calls.
    for schema in (server._COUNCIL_SCHEMA, server._ATLAS_COUNCIL_SCHEMA):
        spec = schema["properties"]["synthesizer"]
        for token in list(oracles.KNOWN) + sorted(oracles.ALIASES):
            assert _matches(spec, token), token
        assert _matches(spec, "atlas:openai/gpt-5.6-sol")
        assert _matches(spec, "ollama:kimi-k2.7-code:cloud")
        assert _matches(spec, "ali:qwen3.8-max")  # a single ali: model is a valid synthesizer
        assert not _matches(spec, "gpt-4")
        assert not _matches(spec, "atlas:")
