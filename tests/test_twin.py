"""The `twin` model group — one operator token, a dual Fable/Opus 5 invocation.

A group differs from an alias in arity: an alias renames ONE model, a group takes
several seats. So the two things worth pinning are that it expands everywhere a
LIST of models is accepted (council fan-out, chain pipeline, tier preset), and
that a single-model slot refuses it loudly instead of quietly keeping member one.
"""

from __future__ import annotations

import asyncio
import re

import pytest

import ask_fable.server as server
from ask_fable import oracles


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_deepseek(monkeypatch):
    # The default council grows a member when this key is set; the twin group
    # must be unaffected by it either way.
    monkeypatch.delenv("ASK_FABLE_DEEPSEEK_API_KEY", raising=False)


def test_group_members_spellings():
    assert oracles.group_members("twin") == ("fable", "opus")
    for spelling in ("twins", "Twin Flames", "TWIN-FLAME", "twin_flames", " twinflame "):
        assert oracles.group_members(spelling) == ("fable", "opus"), spelling
    # everything that is not a group still reads as "no group"
    for tok in ("fable", "opus", "m3", "atlas:zai-org/glm-5.2", ""):
        assert oracles.group_members(tok) is None, tok


def test_resolve_expands_the_group_in_a_fan_out():
    assert oracles.resolve(["twin"]) == (["fable", "opus"], [])
    # composes with ordinary members, and the canonical cheap-first order still wins
    assert oracles.resolve(["minimax", "twin flames"]) == (["fable", "opus", "minimax"], [])
    # de-dupe is unchanged: naming a member twice is still one seat
    assert oracles.resolve(["twin", "opus"]) == (["fable", "opus"], [])


def test_resolve_ordered_expands_to_two_stages():
    # in a chain the group is two stages in group order — fable drafts, opus decides
    assert oracles.resolve_ordered(["twin"]) == (["fable", "opus"], [])
    assert oracles.resolve_ordered(["m3", "twin"]) == (["minimax", "fable", "opus"], [])
    # order and repeats survive: the group can appear twice like any other stage
    assert oracles.resolve_ordered(["twin", "glm", "twin"]) == (
        ["fable", "opus", "glm", "fable", "opus"], [],
    )


def test_twin_is_a_tier_preset():
    assert oracles.tier_models("twin") == ["fable", "opus"]
    assert oracles.tier_models("Twin Flames") == ["fable", "opus"]
    assert "twin" in oracles.TIERS
    # a still-unknown tier keeps falling back to default, not to the group
    assert oracles.tier_models("bogus") == ["fable", "minimax"]


def test_group_order_is_the_tuple_order():
    """`GROUPS` values are tuples because in a chain the tuple order IS the stage
    order — fable drafts, opus decides. Pins it so nobody 'tidies' one into a set."""
    for name, members in oracles.GROUPS.items():
        assert isinstance(members, tuple), name
        assert oracles.resolve_ordered([name]) == (list(members), [])


@pytest.mark.parametrize("name", sorted(oracles.GROUPS))
def test_every_group_resolves_with_nothing_unknown(name):
    assert oracles.resolve([name])[1] == []
    assert oracles.resolve_ordered([name])[1] == []


def test_malformed_groups_are_rejected_at_import(monkeypatch):
    """A bad group definition doesn't raise on its own — it silently changes what
    gets asked (an empty group falls through to the DEFAULT council with an empty
    `unknown`; a bad member is reported under a token the caller never typed; a
    nested group is never expanded). `_validate_groups` is what makes each of those
    a loud config error instead."""
    # Each case REPLACES the registry rather than merging into it, so one bad
    # definition can't be masked by an unrelated complaint about a real group.
    cases = [
        ({"ghost": ()}, {}, "is empty"),
        ({"bad": ("fable", "nope")}, {}, "unknown member"),
        ({"twin": ("fable", "opus"), "nest": ("twin", "glm")}, {}, "nests group"),
        ({"fable": ("opus",)}, {}, "shadows a model token"),
        ({"twin": ("fable", "opus")}, {"orphan": "gone"}, "missing group"),
    ]
    for groups, aliases, message in cases:
        monkeypatch.setattr(oracles, "GROUPS", groups)
        monkeypatch.setattr(oracles, "GROUP_ALIASES", aliases)
        with pytest.raises(ValueError, match=message):
            oracles._validate_groups()
        monkeypatch.undo()


def test_council_schema_advertises_every_group_spelling():
    """A strictly-validating MCP host only passes what the enum lists, so every
    spelling the resolver honors has to be declared."""
    spec = server._COUNCIL_SCHEMA["properties"]["models"]["items"]

    def accepts(token: str) -> bool:
        return any(
            ("enum" in b and token in b["enum"]) or ("pattern" in b and re.match(b["pattern"], token))
            for b in spec["anyOf"]
        )

    for token in list(oracles.GROUPS) + sorted(oracles.GROUP_ALIASES):
        assert accepts(token), token
    assert "twin" in server._COUNCIL_SCHEMA["properties"]["tier"]["enum"]


def test_synthesizer_slot_refuses_the_group():
    tok, err = server._resolve_synth_token("twin")
    assert tok is None
    assert err is not None and "fable + opus" in err and "single" in err
    # a lone member is still perfectly fine there
    assert server._resolve_synth_token("opus") == ("opus", None)


@pytest.mark.parametrize("role", ["proposer", "opponent", "adjudicator"])
def test_debate_roles_refuse_the_group(monkeypatch, role):
    monkeypatch.setenv("ASK_FABLE_QUIET", "1")
    monkeypatch.setattr(server.guard, "check", lambda q, c="": (True, ""))
    out = _run(server._handle_debate({"question": "x or y?", role: "twin flames"}))
    assert out["status"] == "error" and out["kind"] == "bad_args"
    assert f"the {role}" in out["detail"] and "fable + opus" in out["detail"]
