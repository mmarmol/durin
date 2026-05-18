"""V6: Self-review loop (Camino B).

V3 (Critic alone) and V4 (Critic + Criteria) both failed in V5. Both used a
SEPARATE LLM call to verify the agent's work — and that call either had clean
context (no idea what's important) or narrow criteria (constraining).

V6 tests a fundamentally different approach: SAME agent, SAME context, but
structured self-review forced before complete_goal. The agent reflects on its
own work with full context of everything it did.

Hypothesis: the agent has the context to find gaps when prompted to actually
look. Forcing a structured walkthrough may surface what it missed.

Conditions tested across the 3 scenarios:
  - baseline:         no intervention
  - with_self_review: complete_goal triggers a structured self-check; agent
                      must either justify completion or continue working

Trials: 2 per condition per scenario.
"""

from __future__ import annotations

import asyncio
import copy
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from openai import AsyncOpenAI

from run_experiment_v3_critic import (  # type: ignore
    AGENT_SYSTEM,
    TOOLS,
    Workspace,
    _MAX_ITERATIONS,
    _MODEL,
    _SCENARIOS_DIR,
    IterationTrace,
    RunTrace,
    _load_api_key,
    execute_tool_call,
    judge_run,
    llm_chat,
    run_condition,
    serialize_trace,
)
from run_experiment_v5_multiscenario import SCENARIOS  # type: ignore

N_TRIALS = 2
_MAX_SELF_REVIEWS = 2
_API_BASE = "https://api.z.ai/api/coding/paas/v4"


# --- Self-review prompt ---

SELF_REVIEW_PROMPT = """\
You called complete_goal. Before this is accepted, conduct a structured self-review.

Answer these questions plainly in your next message — and only AFTER answering, \
decide what to do next:

1. THE TASK: restate the original task in one sentence. What is success?

2. WHAT YOU CHANGED: list each file you edited and what changed in it.

3. FILES YOU DID NOT EDIT (from the workspace): for each, briefly say why you \
chose not to edit it. Did anything in those files suggest changes you skipped?

4. ROOT CAUSE vs SYMPTOM: are you fixing the actual root cause, or just \
patching the surface? How do you know?

5. WHAT COULD BE WRONG: think like a senior reviewer who hasn't seen your \
work. What's the most likely way your fix is incomplete?

After answering all five questions:
- If your review reveals gaps, KEEP WORKING. Use read_file / edit_file to \
address them. Then call complete_goal again when truly done.
- If after honest review you are confident the work is complete, call \
complete_goal again with a recap that addresses any concerns you raised."""


# --- Self-review condition runner ---

async def run_condition_self_review(
    client: AsyncOpenAI,
    goal: str,
    available_files: list[str],
    scenario_dir: Path,
) -> RunTrace:
    """Agent loop where complete_goal triggers a self-review before succeeding."""
    t_start = time.time()
    trace = RunTrace(condition="with_self_review", scenario=scenario_dir.name)
    workspace = Workspace.from_dir(scenario_dir, available_files)

    messages: list[dict] = [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": goal},
    ]

    self_reviews_used = 0

    for iteration in range(1, _MAX_ITERATIONS + 1):
        iter_trace = IterationTrace(
            iteration=iteration,
            messages_at_call=copy.deepcopy(messages),
        )
        t_iter = time.time()
        msg = await llm_chat(client, messages, tools=TOOLS)
        iter_trace.duration_ms = (time.time() - t_iter) * 1000

        assistant_dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        iter_trace.assistant_message = copy.deepcopy(assistant_dict)
        messages.append(assistant_dict)

        if not msg.tool_calls:
            trace.stop_reason = "assistant_stopped_no_tools"
            trace.iterations.append(iter_trace)
            break

        completed_this_iter = False
        completion_recap = ""

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}

            iter_trace.tool_calls.append({"id": tc.id, "name": name, "arguments": args})

            if name == "complete_goal":
                completion_recap = args.get("recap", "")

                if self_reviews_used < _MAX_SELF_REVIEWS:
                    # Trigger self-review: block completion, inject prompt
                    self_reviews_used += 1
                    iter_trace.tool_results.append({
                        "tool_call_id": tc.id, "name": name,
                        "content": SELF_REVIEW_PROMPT,
                        "self_review_triggered": True,
                        "review_number": self_reviews_used,
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": SELF_REVIEW_PROMPT,
                    })
                    continue

                # Max self-reviews reached — allow completion
                iter_trace.tool_results.append({
                    "tool_call_id": tc.id, "name": name,
                    "content": "complete_goal accepted (self-review budget exhausted).",
                })
                completed_this_iter = True
                trace.completion_recap = completion_recap
                break

            result_content = await execute_tool_call(workspace, name, args)
            iter_trace.tool_results.append({
                "tool_call_id": tc.id, "name": name, "content": result_content[:3000],
            })
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_content})

        trace.iterations.append(iter_trace)

        if completed_this_iter:
            trace.completed = True
            trace.stop_reason = "complete_goal"
            break
    else:
        trace.stop_reason = "max_iterations"

    trace.final_edited_files = dict(workspace.files)
    trace.total_duration_ms = (time.time() - t_start) * 1000
    return trace


# --- Main ---

async def main() -> None:
    api_key = _load_api_key()
    client = AsyncOpenAI(api_key=api_key, base_url=_API_BASE)

    print(f"V6 SELF-REVIEW ({N_TRIALS} trials × 2 conditions × {len(SCENARIOS)} scenarios)")
    print(f"Model: {_MODEL}\n")

    results: dict[tuple[str, str], list[int]] = {}
    all_traces: list[dict] = []

    for scenario in SCENARIOS:
        s_name = scenario["name"]
        s_dir = _SCENARIOS_DIR / scenario["dir"]
        files = scenario["files"]
        goal = scenario["goal"]
        ground_truth = scenario["ground_truth"]

        import run_experiment_v3_critic as v3mod
        v3mod.GROUND_TRUTH_SCENARIO_2 = ground_truth

        print(f"\n{'='*70}")
        print(f"SCENARIO: {s_name}")
        print(f"{'='*70}")

        for cond in ("baseline", "with_self_review"):
            key = (s_name, cond)
            results[key] = []
            for trial in range(1, N_TRIALS + 1):
                if cond == "with_self_review":
                    trace = await run_condition_self_review(client, goal, files, s_dir)
                else:
                    trace = await run_condition(client, goal, files, s_dir, "baseline")
                await judge_run(client, trace, goal)
                results[key].append(trace.judge_score)
                t = serialize_trace(trace)
                t["scenario"] = s_name
                t["trial"] = trial
                all_traces.append(t)

                # Count self-review triggers
                sr_triggers = sum(
                    1 for it in trace.iterations
                    for tr in it.tool_results
                    if isinstance(tr, dict) and tr.get("self_review_triggered")
                )
                sr_str = f" reviews={sr_triggers}" if cond == "with_self_review" else ""
                print(f"  {cond:<18} trial {trial}: {trace.judge_score}/5  iters={len(trace.iterations)}{sr_str}")

    # Aggregate
    print(f"\n{'='*70}")
    print("AGGREGATE")
    print(f"{'='*70}")

    print(f"\n{'Scenario':<32} {'Condition':<20} {'Scores':<12} {'Avg':<6}")
    print("-" * 80)
    for scenario in SCENARIOS:
        s = scenario["name"]
        for cond in ("baseline", "with_self_review"):
            scores = results.get((s, cond), [])
            avg = statistics.mean(scores) if scores else 0
            print(f"{s:<32} {cond:<20} {str(scores):<12} {avg:.2f}")
        print()

    print("Global averages:")
    for cond in ("baseline", "with_self_review"):
        all_scores = [s for key, scores in results.items() if key[1] == cond for s in scores]
        if all_scores:
            avg = statistics.mean(all_scores)
            try:
                sd = statistics.stdev(all_scores)
            except statistics.StatisticsError:
                sd = 0
            print(f"  {cond:<20} N={len(all_scores)} avg={avg:.2f} stdev={sd:.2f} scores={all_scores}")

    print("\nPer-scenario deltas vs baseline:")
    for scenario in SCENARIOS:
        s = scenario["name"]
        b = statistics.mean(results.get((s, "baseline"), [0]))
        sr = statistics.mean(results.get((s, "with_self_review"), [0]))
        print(f"  {s}:  baseline={b:.2f}  with_self_review={sr:.2f}  delta={sr-b:+.2f}")

    # Self-review trigger summary
    total_triggers = sum(
        1 for t in all_traces if t["condition"] == "with_self_review"
        for it in t.get("iterations", [])
        for tr in it.get("tool_results", [])
        if isinstance(tr, dict) and tr.get("self_review_triggered")
    )
    print(f"\nTotal self-review triggers: {total_triggers}")

    output_path = _SCENARIOS_DIR / "results_v6_self_review.json"
    with open(output_path, "w") as f:
        json.dump({
            "n_trials": N_TRIALS,
            "scenarios": [s["name"] for s in SCENARIOS],
            "results_table": {f"{k[0]}__{k[1]}": v for k, v in results.items()},
            "traces": all_traces,
        }, f, indent=2)
    print(f"\nFull traces: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
