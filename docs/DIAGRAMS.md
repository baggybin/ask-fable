# Decision-flow diagrams

> Part of the [ask-fable README](../README.md).


The diagrams below zoom in on individual orchestration modes. For the current
end-to-end system and request lifecycle, use the two diagrams in
[How it works](../README.md#how-it-works); these lower-level charts are implementation aids.

### 1. The Core Ask Path
The fundamental pathway for asking a single model. Notice how the query is checked against the internal guard rails and SQLite context references before any model inference occurs.

<p align="center">
  <img src="../images/ask_core.png" alt="Core Ask Path Pseudocode Chart">
</p>

### 2. Council Fan-out & Synthesis
When parallel multi-model validation is needed, the `ask_council` mode spins up asynchronous calls to N oracles, parses valid responses, strips their identity (Expert A, Expert B), and tasks Fable with synthesizing an objective outcome.

<p align="center">
  <img src="../images/ask_council.png" alt="Council Synthesis Pseudocode Chart">
</p>

### 3. Sequential Chain Logic
For problems that require iterative refinement (Draft → Critique → Decide), the pipeline sequentially routes responses, tracking output drift and handling stage skips gracefully on model failure.

<p align="center">
  <img src="../images/ask_chain.png" alt="Sequential Chain Logic Chart">
</p>

### 4. Adversarial Debate Mode
The most intense workflow pairs a Proposer and Opponent in multi-round debate. It forces position revision under fire before an anonymized Fable adjudicator evaluates the ledger and resolves the outcome on its merits.

<p align="center">
  <img src="../images/ask_debate.png" alt="Adversarial Debate Flowchart">
</p>

### 5. Triple-Layer Safeguards
Security runs *before* the prompt touches the network. This involves sanity length bounds, allow-list phrase neutralization, denylist checking, and an initial model scope-enforcement query.

<p align="center">
  <img src="../images/guard_layers.png" alt="Triple-Layer Guard Logic">
</p>
