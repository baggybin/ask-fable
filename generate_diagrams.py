import os
import subprocess

os.makedirs('images', exist_ok=True)

mermaid_files = {
    'images/ask_core.mmd': """graph TD
    Start["def ask(Q, context, context_ref):"] --> DB["Load context_ref from SQLite DB"]
    DB --> Missing{"Missing Keys?"}
    Missing -- Yes --> RetNeedsCtx["return needs_context, did_you_mean"]
    Missing -- No --> Guard["status = check_guard(Q + Context)"]
    Guard --> IsGuarded{"status == passed?"}
    IsGuarded -- No --> RetRefused["return REFUSED"]
    IsGuarded -- Yes --> Cache["cached = check_cache(hash(Q+Context))"]
    Cache --> IsCached{"cached?"}
    IsCached -- Yes --> RetCached["return cached_answer"]
    IsCached -- No --> Call["resp = call_claude(Q + Context)"]
    Call --> Parse["sidecar = extract_json(resp)"]
    Parse --> NeedsMore{"sidecar.needs_context?"}
    NeedsMore -- Yes --> LoopCount{"loops > max_needs_context?"}
    LoopCount -- Yes --> Exhausted["return context_exhausted"]
    LoopCount -- No --> ReturnFollowUp["return followup, prompt_user_to_paste"]
    NeedsMore -- No --> Save["save_to_cache(hash, resp)"]
    Save --> ReturnFinal["return answer, sidecar"]
    
    classDef func fill:#f9f,stroke:#333,stroke-width:2px;
    class Start func;
""",

    'images/ask_council.mmd': """graph TD
    Start["def ask_council(Q, models):"] --> Setup["promises = []<br>for model in models:<br>  promises.push(call_oracle(model, Q))"]
    Setup --> Wait["responses = await gather(*promises)"]
    Wait --> Filter["valid_resps = [r for r in responses if r.ok]"]
    Filter --> CheckQ{"len(valid_resps) == 0?"}
    CheckQ -- Yes --> RetErr["return Error('all models failed')"]
    CheckQ -- No --> Anonymize["anon_resps = anonymize(valid_resps)<br>Expert A: ..., Expert B: ..."]
    Anonymize --> Synthesize["synth_prompt = build_synth_prompt(anon_resps)<br>final_answer = call_claude(synth_prompt)"]
    Synthesize --> Consensus["consensus_level = compute_consensus(valid_resps)<br>e.g., strong, partial, divergent"]
    Consensus --> Save["save_to_cache(hash, final_answer)"]
    Save --> Ret["return final_answer, sources=valid_resps, consensus=consensus_level"]

    classDef func fill:#f9f,stroke:#333,stroke-width:2px;
    class Start func;
""",

    'images/ask_chain.mmd': """graph TD
    Start["def ask_chain(Q, pipeline):"] --> Init["history = []<br>draft = None"]
    Init --> Loop["for stage in pipeline:"]
    Loop --> IsFirst{"stage == pipeline[0]?"}
    IsFirst -- Yes --> Draft["draft = call_oracle(stage, Q)<br>history.push(draft)"]
    IsFirst -- No --> Critique["prompt = Q + anonymize(history)<br>draft = call_oracle(stage, prompt)<br>history.push(draft)"]
    Draft --> CheckFail["if draft.failed:<br>  continue (skip stage)"]
    Critique --> CheckFail
    CheckFail --> LoopEnd{"More Stages?"}
    LoopEnd -- Yes --> Loop
    LoopEnd -- No --> FinalCheck{"final_stage.failed?"}
    FinalCheck -- Yes --> Fallback["final_draft = synthesize(history, claude)"]
    FinalCheck -- No --> SetFinal["final_draft = history[-1]"]
    Fallback --> Drift["drift = compute_drift(history)"]
    SetFinal --> Drift
    Drift --> Ret["return final_draft, drift, history"]

    classDef func fill:#f9f,stroke:#333,stroke-width:2px;
    class Start func;
""",

    'images/ask_debate.mmd': """graph TD
    Start["def ask_debate(Q, proposer, opponent, rounds):"] --> Turn1["claims = call_model(proposer, Q, 'decompose_claims')"]
    Turn1 --> Loop["for round in 1..rounds:"]
    Loop --> OppTurn["contests = call_model(opponent, claims, 'contest_or_concede')"]
    OppTurn --> AllConceded{"all_conceded?"}
    AllConceded -- Yes --> ResConceded["resolution = 'conceded'"] --> EndDebate
    AllConceded -- No --> PropTurn["claims = call_model(proposer, contests, 'revise')"]
    PropTurn --> CheckRounds{"round < rounds?"}
    CheckRounds -- Yes --> Loop
    CheckRounds -- No --> AdjTurn["ruling = call_model('fable', claims+contests, 'adjudicate')"]
    AdjTurn --> DecideRes["if agreement: resolution = 'converged'<br>elif stubborn: resolution = 'stalemate'<br>else: resolution = 'adjudicated'"]
    DecideRes --> EndDebate
    EndDebate --> Return["return resolution, ruling, decisive_argument"]

    classDef func fill:#f9f,stroke:#333,stroke-width:2px;
    class Start func;
""",

    'images/guard_layers.mmd': """graph TD
    Start["def check_guard(prompt):"] --> Floor["if len(prompt) < 3 or len(prompt) > 65536:<br>  return false"]
    Floor --> Allow["masked = mask_allowlist(prompt, ALLOWLIST)"]
    Allow --> Deny["if contains_offensive_markers(masked, DENYLIST):<br>  return false"]
    Deny --> Unmask["unmasked = restore_allowlist(masked)"]
    Unmask --> Contract["is_code = ask_fable_fast('Is this software eng?', unmasked)"]
    Contract --> CheckContract{"is_code?"}
    CheckContract -- No --> Refused["return REFUSED"]
    CheckContract -- Yes --> Passed["return PASSED"]

    classDef func fill:#f9f,stroke:#333,stroke-width:2px;
    class Start func;
"""
}

for filename, content in mermaid_files.items():
    with open(filename, 'w') as f:
        f.write(content)
        
    png_filename = filename.replace('.mmd', '.png')
    print(f"Generating {png_filename}...")
    subprocess.run(['npx', '-y', '@mermaid-js/mermaid-cli', '-i', filename, '-o', png_filename, '-b', 'transparent'], check=True)
    print(f"Generated {png_filename}")

print("Done.")
