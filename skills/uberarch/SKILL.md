---
name: uberarch
description: "Invoke for deep, open-ended architectural brainstorming, greenfield ideation, and functionality analysis. When you need to design a system from scratch or rethink a core feature, use this to fan out abstract ideas to the oracles (via ask_council or ask_chain) and get rigorous, multi-model analysis on trade-offs before writing any code."
---

# uberarch

Local architectural ideation and brainstorming protocol leveraging `ask_fable`'s multi-model capabilities. Use this when the problem isn't "how do I implement this plan?" but rather "what should we even build?" or "what are the architectural trade-offs of this new feature?"

Usage: `/uberarch <topic or problem statement>`

## When to use
- **USE for:** Greenfield architecture, new feature brainstorming, analyzing major functionality changes, or ideating alternative approaches to a systemic problem.
- **DON'T use for:** tactical refactors, writing code, or step-by-step implementation plans (use `/uberplan` or `/ubercode` instead).
- **Cost:** High. Brainstorming across multiple models via `ask_council` or `ask_chain` is computationally expensive but pays off by preventing bad architectural decisions early.

## The Strategy

While `uberplan` is about evaluating *concrete steps*, `uberarch` is about exploring *the abstract space*. 

### 1. Build the Landscape Brief
Rather than a strict implementation brief, gather the constraints of the problem space:
- **The Core Problem:** What are we fundamentally trying to solve?
- **The System Context:** What does the surrounding architecture look like? What are the rigid boundaries?
- **The Non-Negotiables:** Are there strict latency, cost, storage, or security constraints?
Save this brief to the server using **`context_write`** so you can reference it across multiple brainstorming turns using `context_ref`.

### 2. The Multi-Model Ideation
Do not just ask for one solution. Force the models to think differently.

Use **`ask_chain`** for sequential red-teaming and refinement:
```python
ask_chain(
  question="Given this problem space, generate 3 completely different architectural paradigms to solve it (e.g. event-driven vs monolith vs decentralized). Criticize the previous stage's paradigms and propose an alternative that covers their blind spots.",
  context_ref="landscape_brief_key",
  pipeline="m3 > glm > deepseek > fable"
)
```
Or use **`ask_council`** for parallel synthesis:
```python
ask_council(
  question="Brainstorm radical, out-of-the-box architectural solutions for this feature. We want 3 divergent approaches with their pros and cons.",
  context_ref="landscape_brief_key",
  models=["fable", "opus", "minimax", "deepseek"]
)
```
*(Note: Escalate to a 4-model council or chain ONLY when the architectural decision is foundational and irreversible. Default to Fable+MiniMax for standard ideation. `opus` is the cheapest way to add a second strong Anthropic voice — and `ask_opus5` is the single-model tool for the same model when you just want one quick take; `glm`/`gemini`/`codex`/`grok`/`kimi` are available too when you want more provider diversity.)*

### 3. Drill Down on Trade-offs
Once you have the divergent paradigms, open a persistent, multi-turn **`ask`** session with Fable to refine the best ideas.
```python
ask(
  question="Let's drill down into Paradigm B (the event-driven approach). If we choose this, what are the cascading failure modes? How do we handle partial updates?",
  context_ref="landscape_brief_key",
  session="uberarch-event-design"
)
```

### 4. The Final Blueprint Synthesis
Once the brainstorming is complete and the trade-offs are mapped, synthesize the findings locally into an Artifact (e.g., `architecture_proposal.md`).
The document should include:
- The chosen architectural paradigm and *why* it won over the alternatives.
- The rejected paradigms and the specific trade-offs that disqualified them.
- The high-level component diagram (use mermaid.js if helpful).
- Unresolved "known unknowns" that require prototyping.

Do NOT start writing project code. `uberarch` concludes when a conceptual blueprint is agreed upon.

## Best Practices for Architectural Prompts
- **Ask for the negative space:** "What is the worst possible way to architect this, and what elements of that bad design are we secretly doing right now?"
- **Force extremes:** "If we had to optimize this purely for latency at the cost of everything else, what does it look like? Now, what if we optimized purely for cost?"
- **Leverage the pipeline:** Use `ask_chain` to have deepseek draft a complex architecture, glm to ruthlessly find its bottlenecks, and Fable to propose a balanced final version.
- **Make them argue:** when two architectures are genuinely close, `ask_debate` puts a proposer and an opponent on a claims ledger and has a third model adjudicate — you get the strongest case for each side instead of a synthesized average that hides the trade-off.
- **Point, don't paste:** `context_pack(files=["src/app.py:1-120", ...])` has the server read the real files into a bundle; pass its key as `context_ref`.
