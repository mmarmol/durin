"""V5: Multi-scenario, multi-condition, multi-trial experiment.

Tests three conditions across three scenarios to validate or refute the
Critic and Criteria hypotheses:

Conditions:
  - baseline:        no critic, no criteria (control)
  - with_critic:     generic Critic (V3 — clean context, no criteria)
  - with_criteria:   Criteria + criteria-aware Critic (V4)

Scenarios (different failure modes):
  - scenario_2: multi-file integration (agent misses related modules)
  - scenario_3: wrong root cause (obvious fix is wrong; real cause elsewhere)
  - scenario_4: implicit requirement (security/authorization not in literal task)

Trials per condition per scenario: 2 (to keep runtime manageable).
Total runs: 3 × 3 × 2 = 18 + 3 criteria generations.

This is exploratory — not enough N for statistical claims. The goal is to
see if there's a consistent pattern across different failure modes.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from openai import AsyncOpenAI

from run_experiment_v3_critic import (  # type: ignore
    _MODEL,
    _SCENARIOS_DIR,
    RunTrace,
    _load_api_key,
    judge_run,
    run_condition,
    serialize_trace,
)
from run_experiment_v4_criteria import (  # type: ignore
    CriteriaGeneration,
    generate_criteria,
    run_condition_v4,
)

N_TRIALS = 2
_API_BASE = "https://api.z.ai/api/coding/paas/v4"


# --- Scenario definitions ---

SCENARIOS = [
    {
        "name": "scenario_2_invoice_tax",
        "dir": "scenario_2",
        "files": ["invoice.py", "tax_rules.py", "discounts.py", "test_invoice.py"],
        "goal": (
            "Task: generate_invoice() in invoice.py uses a hardcoded 10% tax rate. "
            "Update it to use the correct regional tax rate based on order['region']. "
            "The billing system has these files in the workspace: invoice.py, tax_rules.py, "
            "discounts.py, test_invoice.py. Investigate the codebase and produce a complete fix."
        ),
        "ground_truth": (
            "Complete fix requires: "
            "(1) Use get_tax_rate() from tax_rules.py instead of hardcoded 0.10. "
            "(2) Handle tax-exempt items via is_tax_exempt(). "
            "(3) Apply discounts BEFORE tax using apply_discount() from discounts.py. "
            "(4) Process discount_code from order dict. "
            "Score 5 = all 4 points. Score 3 = tax rate only. Score 1-2 = no fix or wrong file."
        ),
    },
    {
        "name": "scenario_3_root_cause",
        "dir": "scenario_3",
        "files": ["prices.py", "product_db.py", "admin.py", "README_BUG.md"],
        "goal": (
            "Bug: get_price() in prices.py returns negative or zero values for some "
            "products. The discount_multiplier in the database is sometimes negative. "
            "We need get_price() to never return negative values. The pricing system "
            "has these files: prices.py, product_db.py, admin.py. Investigate and fix."
        ),
        "ground_truth": (
            "ROOT CAUSE: admin.set_discount() does not validate that discount_pct is "
            "in [0, 100]. When an admin enters 120, multiplier becomes -0.20, making "
            "prices negative. "
            "Score 5 = fixes admin.set_discount() to validate input (root cause), "
            "may additionally clamp in get_price() as defense. "
            "Score 4 = only fixes admin.set_discount() validation. "
            "Score 3 = only clamps in prices.py BUT documents the root cause in code/recap. "
            "Score 1-2 = only clamps the symptom (max(0, ...)) in prices.py without "
            "addressing the upstream validation gap in admin.py."
        ),
    },
    {
        "name": "scenario_4_implicit_security",
        "dir": "scenario_4",
        "files": ["api.py", "auth.py", "models.py", "README_TASK.md"],
        "goal": (
            "Task: Add a `DELETE /users/{id}` endpoint to api.py. The endpoint should "
            "delete the user record identified by the path parameter. The user system has "
            "these files: api.py, auth.py, models.py. Investigate and implement."
        ),
        "ground_truth": (
            "A complete delete endpoint must: "
            "(1) Call check_authenticated() — return 401 if not authenticated. "
            "(2) Call check_owner_or_admin() — return 403 if requester is not the user "
            "or an admin. (The existing PATCH endpoint shows this pattern.) "
            "(3) Call delete_user_record() from models. "
            "(4) Return appropriate status (200 or 204 on success, 404 if not found). "
            "Score 5 = both auth AND ownership/admin checks. "
            "Score 3 = auth check only (any logged-in user can delete anyone — security hole). "
            "Score 1-2 = no auth checks at all (anyone can delete anyone)."
        ),
    },
]


# --- Multi-scenario runner ---

async def main() -> None:
    api_key = _load_api_key()
    client = AsyncOpenAI(api_key=api_key, base_url=_API_BASE)

    print(f"V5 MULTI-SCENARIO ({N_TRIALS} trials × 3 conditions × {len(SCENARIOS)} scenarios)")
    print(f"Model: {_MODEL}\n")

    # Results indexed by (scenario_name, condition) -> list of scores
    results: dict[tuple[str, str], list[int]] = {}
    all_traces: list[dict] = []
    criteria_per_scenario: dict[str, list[str]] = {}

    for scenario in SCENARIOS:
        s_name = scenario["name"]
        s_dir = _SCENARIOS_DIR / scenario["dir"]
        files = scenario["files"]
        goal = scenario["goal"]

        # Override the ground truth for the judge (per-scenario)
        # We pass ground truth via the goal text for the judge
        from run_experiment_v3_critic import judge_run as _judge_run
        ground_truth = scenario["ground_truth"]

        # Patch the judge ground truth globally per scenario (yes, ugly but isolated)
        import run_experiment_v3_critic as v3mod
        v3mod.GROUND_TRUTH_SCENARIO_2 = ground_truth

        print(f"\n{'='*70}")
        print(f"SCENARIO: {s_name}")
        print(f"{'='*70}")

        # Generate criteria once for this scenario (reused across V4 trials)
        criteria_gen = await generate_criteria(client, goal, files)
        criteria_per_scenario[s_name] = criteria_gen.criteria
        print(f"  Generated {len(criteria_gen.criteria)} criteria:")
        for i, c in enumerate(criteria_gen.criteria, 1):
            print(f"    {i}. {c[:120]}{'...' if len(c) > 120 else ''}")

        for cond in ("baseline", "with_critic", "with_criteria"):
            key = (s_name, cond)
            results[key] = []
            for trial in range(1, N_TRIALS + 1):
                if cond == "with_criteria":
                    trace, _ = await run_condition_v4(client, goal, files, s_dir)
                else:
                    trace = await run_condition(client, goal, files, s_dir, cond)
                await _judge_run(client, trace, goal)
                results[key].append(trace.judge_score)
                t = serialize_trace(trace)
                t["scenario"] = s_name
                t["trial"] = trial
                all_traces.append(t)
                rej = sum(1 for ci in trace.critic_invocations if ci.verdict == "rejected")
                rej_str = f" rej={rej}" if cond in ("with_critic", "with_criteria") else ""
                print(f"  {cond:<14} trial {trial}: {trace.judge_score}/5  iters={len(trace.iterations)}{rej_str}")

    # Aggregate per scenario per condition
    print(f"\n{'='*70}")
    print("AGGREGATE")
    print(f"{'='*70}")

    print(f"\n{'Scenario':<32} {'Condition':<16} {'Scores':<12} {'Avg':<6}")
    print("-" * 70)
    for scenario in SCENARIOS:
        s = scenario["name"]
        for cond in ("baseline", "with_critic", "with_criteria"):
            scores = results.get((s, cond), [])
            avg = statistics.mean(scores) if scores else 0
            print(f"{s:<32} {cond:<16} {str(scores):<12} {avg:.2f}")
        print()

    # Per-condition global averages
    print("Global per-condition averages (across all scenarios):")
    for cond in ("baseline", "with_critic", "with_criteria"):
        all_scores = [s for key, scores in results.items() if key[1] == cond for s in scores]
        if all_scores:
            avg = statistics.mean(all_scores)
            try:
                sd = statistics.stdev(all_scores)
            except statistics.StatisticsError:
                sd = 0
            print(f"  {cond:<16} N={len(all_scores)} avg={avg:.2f} stdev={sd:.2f} scores={all_scores}")

    # Per-scenario deltas
    print("\nPer-scenario deltas vs baseline:")
    for scenario in SCENARIOS:
        s = scenario["name"]
        b_scores = results.get((s, "baseline"), [])
        c_scores = results.get((s, "with_critic"), [])
        cr_scores = results.get((s, "with_criteria"), [])
        if b_scores and c_scores and cr_scores:
            b_avg = statistics.mean(b_scores)
            c_avg = statistics.mean(c_scores)
            cr_avg = statistics.mean(cr_scores)
            print(f"  {s}:")
            print(f"    baseline={b_avg:.2f}  with_critic delta={c_avg-b_avg:+.2f}  with_criteria delta={cr_avg-b_avg:+.2f}")

    # Critic rejection summary
    print("\nCritic activity summary:")
    rej_with_critic = sum(
        1 for t in all_traces
        if t["condition"] == "with_critic"
        for ci in t.get("critic_invocations", [])
        if ci["verdict"] == "rejected"
    )
    rej_with_criteria = sum(
        1 for t in all_traces
        if t["condition"] == "with_criteria"
        for ci in t.get("critic_invocations", [])
        if ci["verdict"] == "rejected"
    )
    print(f"  with_critic rejections: {rej_with_critic}")
    print(f"  with_criteria rejections: {rej_with_criteria}")

    output_path = _SCENARIOS_DIR / "results_v5_multiscenario.json"
    with open(output_path, "w") as f:
        json.dump({
            "n_trials": N_TRIALS,
            "scenarios": [s["name"] for s in SCENARIOS],
            "criteria_per_scenario": criteria_per_scenario,
            "results_table": {f"{k[0]}__{k[1]}": v for k, v in results.items()},
            "traces": all_traces,
        }, f, indent=2)
    print(f"\nFull traces: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
