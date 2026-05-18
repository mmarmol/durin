# Hypothesis Test Suite (V1 – V8)

Experimental evidence behind the May 2026 prune. Every claim in
`docs/02_bitacora.md` and `docs/06_log_experiments.md` traces back to a
script and a JSON result file in this folder.

**Why kept**: When evaluating a new agent-layer idea, the burden of proof
should be empirical. These scripts are the toolkit to reproduce the
refutations — and the templates for testing new ideas under the same
conditions.

---

## Experimental progression

| Version | Script | Hypothesis tested | Outcome |
|---|---|---|---|
| V1 | `run_experiment.py` | Multi-turn challenge prompt before completion improves exploration | Identified gaps consistently but degraded final fix (empty response artifact) |
| V2 | `run_experiment_v2.py` | Integrated challenge (single LLM call) — same goal, cleaner format | Token competition: challenge cannibalizes fix output. Refuted. |
| V3 | `run_experiment_v3_critic.py` | Pre-completion Critic with clean context (Devin pattern) | Approved 10/12, 2 rejections without measurable effect. No-signal. |
| V3+ | `run_experiment_v3_multitrial.py` | Same at temp=0 with N=3 trials per condition | Critic never rejected. Variance is inherent (3, 5, 3 baseline scores). |
| V4 | `run_experiment_v4_criteria.py` | Critic + auto-generated acceptance criteria | -1.16 pts vs baseline. Narrow criteria constrain agent. |
| V5 | `run_experiment_v5_multiscenario.py` | All conditions across 3 distinct failure modes | Confirmed V3/V4 results generalize across scenarios. |
| V6 | `run_experiment_v6_self_review.py` | Camino B: structured self-review by same agent before complete_goal | 12/12 triggered, 0 score change. Same-model self-verification refuted. |
| V7 | `run_experiment_v7_durin_components.py` | Real Durin PlanHook with real pytest + real disk | Gate never blocked, escalation never fired, scenario_3 HURT by -2pts. |
| V8 | `run_experiment_v8_combos.py` | All Durin hooks isolated and combined (5 conditions × 3 scenarios) | All conditions ≤ baseline. Posture+plan combo worst (-0.67 avg). |

---

## File map

```
hypothesis_test/
├── run_experiment.py                       V1 script
├── run_experiment_v2.py                    V2 script
├── run_experiment_v3_critic.py             V3 script
├── run_experiment_v3_multitrial.py         V3 multi-trial runner
├── run_experiment_v4_criteria.py           V4 script
├── run_experiment_v5_multiscenario.py      V5 script
├── run_experiment_v6_self_review.py        V6 script
├── run_experiment_v7_durin_components.py   V7 script (uses real Durin PlanHook)
├── run_experiment_v8_combos.py             V8 script (5-condition combos)
├── results.json                            V1 results
├── results_v2.json                         V2 results
├── results_v3_critic.json                  V3 results
├── results_v3_multitrial.json              V3 multi-trial results
├── results_v4_criteria.json                V4 results
├── results_v5_multiscenario.json           V5 results
├── results_v6_self_review.json             V6 results
├── results_v7_durin_components.json        V7 results (Durin components)
├── results_v8_combos.json                  V8 results (all combos)
├── scenario_1/                             Notification cache bug (multi-file)
├── scenario_2/                             Invoice tax integration
├── scenario_3/                             Root cause vs symptom (pricing)
└── scenario_4/                             Implicit security (DELETE endpoint)
```

Each `scenario_*/` directory contains the source files agents work on plus
a test file (`test_*.py`) that pytest runs to verify the fix.

---

## How to reproduce

Each script is self-contained Python. To re-run an experiment:

```bash
export ZAI_API_KEY=...   # or have it in ~/.hermes/.env
.venv/bin/python scripts/hypothesis_test/run_experiment_v8_combos.py
```

Most scripts produce a corresponding `results_v*.json` with the full trace
of every LLM call, tool call, and judge verdict. V7 and V8 use a custom
agent loop that imports real Durin modules (`PlanHook`, `PostureHook`,
etc.) and writes/reads/execs against a real temp directory. V1–V6 use
isolated LLM calls without Durin's stack.

---

## How to read the JSON results

V7/V8 traces capture per-iteration detail:

```jsonc
{
  "scenario": "scenario_3_root_cause",
  "condition": "plan_posture",
  "judge_score": 3,
  "judge_reasoning": "Only clamps symptom in prices.py...",
  "total_tokens_input": 7972,
  "total_tokens_output": 905,
  "plan_state_final": { "tier": "plan", "cycle_count": 1, ... },
  "iterations": [
    {
      "iter": 1,
      "phase_before": null,
      "temperature": 0.0,
      "tools": ["set_execution_mode"],
      "tokens_in": 1234,
      "tokens_out": 56
    },
    ...
  ]
}
```

V1–V4 traces capture full prompts and assistant responses (raw LLM I/O).

---

## When to add a new experiment

Following the rules in `docs/02_bitacora.md`:

1. Pick a hypothesis that has industrial or academic precedent.
2. Design the test so that **baseline can fail** (no ceiling effect).
3. Use ≥3 trials per condition.
4. Use real verification (pytest on actual files), not just LLM-as-judge.
5. Add a `run_experiment_v{N}_*.py` script following the V7/V8 structure.
6. After running, update `docs/06_log_experiments.md` with results and
   `docs/02_bitacora.md` if a component is being refuted.

---

Last updated: 2026-05-18
