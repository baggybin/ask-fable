"""Scope-contract parity across the system prompts and tool descriptions.

The operator's scope policy: conceptual/brainstorming questions are explicitly
allowed; the ONLY two refusal categories are (1) direct offensive-security asks
in the question itself and (2) non-software domain knowledge. These tests pin
that policy so a future prompt edit can't silently reintroduce a third refusal
category or drop the brainstorming allowance from one tool's wording.
"""

from __future__ import annotations

from ask_fable import prompts


def test_system_prompt_allows_conceptual_and_generative_work():
    # normalize whitespace — the triple-quoted literal wraps mid-sentence
    p = " ".join(prompts.FABLE_SYSTEM_PROMPT.split())
    assert "brainstorming" in p.lower()
    assert "before any code exists" in p
    assert "A missing code context is NOT a reason to refuse" in p
    # generative mode: a menu of distinct ideas is the deliverable
    assert "GENERATIVE" in p


def test_system_prompt_has_exactly_two_refusal_categories():
    p = prompts.FABLE_SYSTEM_PROMPT
    # category 3 ("not related to the agent's software/engineering work") is gone
    assert "not related to the agent" not in p
    assert "  1." in p and "  2." in p and "  3." not in p
    # category 1 triggers on the question itself, not the code's subject matter
    assert "question ITSELF" in p
    assert "security-related is NOT a reason to refuse" in p


def test_oracle_prompt_parity():
    # every non-Fable bridge sends the same scope contract
    assert prompts.ORACLE_SYSTEM_PROMPT is prompts.FABLE_SYSTEM_PROMPT
    assert prompts.MINIMAX_SYSTEM_PROMPT is prompts.ORACLE_SYSTEM_PROMPT


def test_synth_prompt_preserves_brainstorm_diversity():
    p = prompts.SYNTH_SYSTEM_PROMPT
    assert "GENERATIVE" in p
    assert "UNION" in p
    assert "Do not collapse a brainstorm" in p


_SINGLE_MODEL_DESCRIPTIONS = (
    "ASK_M3_TOOL_DESCRIPTION",
    "ASK_GLM_TOOL_DESCRIPTION",
    "ASK_DEEPSEEK_TOOL_DESCRIPTION",
    "ASK_GEMINI_TOOL_DESCRIPTION",
    "ASK_CODEX_TOOL_DESCRIPTION",
    "ASK_OLLAMA_TOOL_DESCRIPTION",
    "ASK_ATLAS_TOOL_DESCRIPTION",
)

_ALL_ASK_DESCRIPTIONS = _SINGLE_MODEL_DESCRIPTIONS + (
    "ASK_TOOL_DESCRIPTION",
    "ASK_COUNCIL_TOOL_DESCRIPTION",
    "ASK_OLLAMA_COUNCIL_TOOL_DESCRIPTION",
    "ASK_CHAIN_TOOL_DESCRIPTION",
    "ASK_DEBATE_TOOL_DESCRIPTION",
)


def test_single_model_descriptions_share_the_conceptual_sentence():
    for name in _SINGLE_MODEL_DESCRIPTIONS:
        desc = getattr(prompts, name)
        assert "conceptual engineering" in desc, name
        assert "brainstorming/ideas for future code" in desc, name
        assert "about existing code" in desc, name


def test_every_ask_description_names_only_the_two_refusal_categories():
    for name in _ALL_ASK_DESCRIPTIONS:
        desc = getattr(prompts, name)
        assert "offensive-security" in desc, name
        assert "non-software domain knowledge" in desc, name
        # the old blanket wording that read as banning defensive work is gone
        assert "cybersecurity/attacks" not in desc, name
        # nothing threatens refusal for missing context
        assert "may be refused" not in desc, name


def test_server_instructions_scope():
    p = prompts.SERVER_INSTRUCTIONS
    assert "conceptual/brainstorming" in p
    assert "OFFENSIVE vocab tripped the guard" in p
    assert "cybersecurity/attacks" not in p


# ── Domain boundary: biology blocked, neuro/cogsci/AI/CS allowed ─────────

def test_system_prompt_explicitly_allows_neuroscience():
    p = prompts.FABLE_SYSTEM_PROMPT
    assert "neuroscience" in p.lower()
    assert "are part of software engineering" in p.lower()
    assert "are answered" in p.lower()


def test_system_prompt_explicitly_refuses_biology():
    p = prompts.FABLE_SYSTEM_PROMPT
    assert "biology" in p.lower()
    assert "medicine are refused" in p.lower()


def test_system_prompt_allows_cognitive_science():
    p = prompts.FABLE_SYSTEM_PROMPT
    assert "cognitive science" in p.lower()


def test_system_prompt_explicitly_allows_ai_ml_cs():
    p = prompts.FABLE_SYSTEM_PROMPT
    assert "AI/ML" in p
    assert "computer science" in p.lower()


def test_every_ask_description_carries_new_scope_wording():
    for name in _ALL_ASK_DESCRIPTIONS:
        desc = getattr(prompts, name)
        assert "biology" in desc.lower(), f"{name} missing biology mention"
        assert "in-scope" in desc.lower(), f"{name} missing in-scope wording"


def test_server_instructions_carries_decision_ladder():
    p = prompts.SERVER_INSTRUCTIONS
    assert "Decision ladder" in p
    assert "Double-strike rule" in p
    assert "Answer it yourself" in p


def test_server_instructions_carries_session_hygiene():
    p = prompts.SERVER_INSTRUCTIONS
    assert 'session="default"' in p or "session=`\"default\"`" in p or 'session \\"default\\"' in p
    assert "contexts bleed between runs" in p


def test_server_instructions_carries_reframe_dont_retry():
    p = prompts.SERVER_INSTRUCTIONS
    assert "Reframe, don't retry" in p
    assert "deterministic" in p.lower()
