"""V3 multi-trial: measure variability across N runs.

The single-run V3 result showed: baseline=5/5, with_critic=3/5. But the divergence
came from LLM stochasticity in the edit step, not from the Critic blocking anything
(the Critic approved on first attempt).

This script runs N trials of each condition to separate signal from noise.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
from pathlib import Path

# Reuse the single-run machinery
sys.path.insert(0, str(Path(__file__).parent))
from run_experiment_v3_critic import (  # type: ignore
    GROUND_TRUTH_SCENARIO_2,
    _MODEL,
    _SCENARIOS_DIR,
    _load_api_key,
    judge_run,
    run_condition,
    serialize_trace,
)
from openai import AsyncOpenAI

N_TRIALS = 3  # Per condition (at temp=0 we expect determinism — 3 trials is enough to verify)


async def main() -> None:
    api_key = _load_api_key()
    client = AsyncOpenAI(api_key=api_key, base_url="https://api.z.ai/api/coding/paas/v4")

    scenario_dir = _SCENARIOS_DIR / "scenario_2"
    available_files = ["invoice.py", "tax_rules.py", "discounts.py", "test_invoice.py"]
    goal = (
        "Task: generate_invoice() in invoice.py uses a hardcoded 10% tax rate. "
        "Update it to use the correct regional tax rate based on order['region']. "
        "The billing system has these files in the workspace: invoice.py, tax_rules.py, "
        "discounts.py, test_invoice.py. Investigate the codebase and produce a complete fix."
    )

    print(f"V3 MULTI-TRIAL ({N_TRIALS} trials per condition, scenario_2)")
    print(f"Model: {_MODEL}\n")

    all_scores: dict[str, list[int]] = {"baseline": [], "with_critic": []}
    all_traces: dict[str, list[dict]] = {"baseline": [], "with_critic": []}
    critic_rejection_counts: list[int] = []

    for trial in range(1, N_TRIALS + 1):
        print(f"=== TRIAL {trial}/{N_TRIALS} ===")

        # Baseline
        b = await run_condition(client, goal, available_files, scenario_dir, "baseline")
        await judge_run(client, b, goal)
        all_scores["baseline"].append(b.judge_score)
        all_traces["baseline"].append(serialize_trace(b))
        print(f"  baseline:    {b.judge_score}/5  iters={len(b.iterations)}  ({b.stop_reason})")

        # With critic
        c = await run_condition(client, goal, available_files, scenario_dir, "with_critic")
        await judge_run(client, c, goal)
        all_scores["with_critic"].append(c.judge_score)
        all_traces["with_critic"].append(serialize_trace(c))
        rejections = sum(1 for ci in c.critic_invocations if ci.verdict == "rejected")
        critic_rejection_counts.append(rejections)
        verdicts = [ci.verdict for ci in c.critic_invocations]
        print(f"  with_critic: {c.judge_score}/5  iters={len(c.iterations)}  critic={verdicts}")

    print(f"\n{'='*60}")
    print("AGGREGATE RESULTS")
    print(f"{'='*60}")
    for cond in ("baseline", "with_critic"):
        scores = all_scores[cond]
        avg = statistics.mean(scores)
        try:
            sd = statistics.stdev(scores)
        except statistics.StatisticsError:
            sd = 0.0
        print(f"\n  {cond}:")
        print(f"    scores: {scores}")
        print(f"    avg: {avg:.2f}  stdev: {sd:.2f}")
        print(f"    min: {min(scores)}  max: {max(scores)}")
        print(f"    distribution: 5={scores.count(5)} 4={scores.count(4)} 3={scores.count(3)} 2={scores.count(2)} 1={scores.count(1)}")

    print(f"\n  Critic rejections per trial: {critic_rejection_counts}")
    print(f"  Total critic rejections: {sum(critic_rejection_counts)}")

    delta = statistics.mean(all_scores["with_critic"]) - statistics.mean(all_scores["baseline"])
    print(f"\n  Delta (with_critic - baseline): {delta:+.2f}")

    # Variability comparison
    baseline_range = max(all_scores["baseline"]) - min(all_scores["baseline"])
    critic_range = max(all_scores["with_critic"]) - min(all_scores["with_critic"])
    print(f"  Score range — baseline: {baseline_range}, with_critic: {critic_range}")

    if abs(delta) < max(baseline_range, critic_range) / 2:
        print("\n  VERDICT: Delta is within variability range — no clear signal from Critic.")
    elif delta > 0:
        print(f"\n  VERDICT: Critic improves outcomes by {delta:+.2f} points on average.")
    else:
        print(f"\n  VERDICT: Critic appears to hurt outcomes by {delta:.2f} points on average.")

    output_path = _SCENARIOS_DIR / "results_v3_multitrial.json"
    with open(output_path, "w") as f:
        json.dump({
            "n_trials": N_TRIALS,
            "scores": all_scores,
            "critic_rejection_counts": critic_rejection_counts,
            "baseline_traces": all_traces["baseline"],
            "with_critic_traces": all_traces["with_critic"],
        }, f, indent=2)
    print(f"\nFull traces: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
