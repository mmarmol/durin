"""V4: Acceptance criteria + Critic that checks each one.

V3 (Critic alone) failed: Critic approved 3/3 incomplete fixes because it
didn't know what to look for. This version:

1. BEFORE work starts: generate explicit acceptance criteria from goal + workspace
2. Agent receives criteria as part of the goal (knows what success looks like)
3. Critic at complete_goal checks each criterion explicitly

Two conditions, 3 trials each at temp=0:
  A) BASELINE: same as V3 baseline (no criteria, no critic)
  B) WITH_CRITERIA: criteria generated, shown to agent, Critic verifies each

Full traces captured.
"""

from __future__ import annotations

import asyncio
import copy
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from openai import AsyncOpenAI

from run_experiment_v3_critic import (  # type: ignore
    AGENT_SYSTEM,
    GROUND_TRUTH_SCENARIO_2,
    TOOLS,
    Workspace,
    _MAX_CRITIC_REJECTIONS,
    _MAX_ITERATIONS,
    _MODEL,
    _SCENARIOS_DIR,
    CriticInvocation,
    IterationTrace,
    RunTrace,
    _load_api_key,
    execute_tool_call,
    judge_run,
    llm_chat,
    run_condition,
    serialize_trace,
)

N_TRIALS = 3
_API_BASE = "https://api.z.ai/api/coding/paas/v4"


# --- Acceptance criteria generation ---

CRITERIA_SYSTEM = """\
You generate acceptance criteria for software engineering tasks.

Given a task description and the list of files in the workspace, list specific, \
verifiable criteria that a COMPLETE fix must satisfy.

Be skeptical. Consider:
- The literal request
- Integration points implied by the workspace structure
- Edge cases the task hints at
- Backward compatibility unless explicitly broken

Each criterion must be:
- SPECIFIC: name concrete functions, files, or behaviors
- VERIFIABLE: can be checked by inspecting the final code
- DERIVED: traceable to the task or workspace structure (don't invent requirements)

Output STRICT JSON:
{
  "criteria": [
    "criterion 1 — specific behavior",
    "criterion 2 — ...",
    ...
  ]
}

Aim for 3-7 criteria. Don't pad."""


@dataclass
class CriteriaGeneration:
    prompt_messages: list[dict] = field(default_factory=list)
    response_raw: str = ""
    criteria: list[str] = field(default_factory=list)
    duration_ms: float = 0.0


async def generate_criteria(
    client: AsyncOpenAI, goal: str, file_list: list[str]
) -> CriteriaGeneration:
    """Generate explicit acceptance criteria from goal + workspace structure."""
    t0 = time.time()
    user_msg = (
        f"## TASK\n{goal}\n\n"
        f"## WORKSPACE FILES (names only)\n{', '.join(file_list)}\n\n"
        "Generate acceptance criteria. Output STRICT JSON only."
    )
    messages = [
        {"role": "system", "content": CRITERIA_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    gen = CriteriaGeneration(prompt_messages=copy.deepcopy(messages))
    msg = await llm_chat(client, messages, temperature=0.0)
    gen.response_raw = msg.content or ""

    text = gen.response_raw
    if "```" in text:
        for p in text.split("```"):
            stripped = p.strip()
            if stripped.startswith("json"):
                text = stripped[4:].strip()
                break
            if stripped.startswith("{"):
                text = stripped
                break
    try:
        data = json.loads(text)
        gen.criteria = list(data.get("criteria", []))
    except json.JSONDecodeError:
        gen.criteria = []

    gen.duration_ms = (time.time() - t0) * 1000
    return gen


# --- Critic that checks each criterion ---

CRITERIA_CRITIC_SYSTEM = """\
You verify whether code changes meet a checklist of acceptance criteria.

You will be shown:
1. The ORIGINAL TASK
2. The ACCEPTANCE CRITERIA (a checklist that MUST be satisfied)
3. The CODE CHANGES made by the engineer
4. The list of WORKSPACE FILES that were available
5. The engineer's RECAP

For EACH criterion, determine: met (true) or not_met (false), with concrete \
evidence from the code.

Approve only if ALL criteria are met. Otherwise reject with specific gaps.

Output STRICT JSON:
{
  "criteria_status": [
    {"criterion": "...", "met": true/false, "evidence": "specific quote or observation"},
    ...
  ],
  "verdict": "approved" | "rejected",
  "gaps": ["concrete action item for each unmet criterion"]
}

Be strict but fair. If a criterion is partially addressed, mark it as not_met \
and explain what's missing."""


async def run_criteria_critic(
    client: AsyncOpenAI,
    goal: str,
    criteria: list[str],
    workspace: Workspace,
    available_files: list[str],
    recap: str,
    iteration: int,
) -> CriticInvocation:
    """Run the Critic with explicit criteria to check."""
    t0 = time.time()

    edited_contents = "\n\n".join(
        f"### {e['path']} (FINAL STATE)\n```python\n{workspace.files[e['path']]}\n```"
        for e in workspace.edits
        if e['path'] in workspace.files
    ) or "(no files were edited)"

    criteria_block = "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))

    user_msg = (
        f"## ORIGINAL TASK\n{goal}\n\n"
        f"## ACCEPTANCE CRITERIA (checklist)\n{criteria_block}\n\n"
        f"## WORKSPACE FILES AVAILABLE\n{', '.join(available_files)}\n\n"
        f"## CODE CHANGES MADE\n{edited_contents}\n\n"
        f"## ENGINEER'S RECAP\n{recap}\n\n"
        "Check each criterion. Output STRICT JSON only."
    )

    messages = [
        {"role": "system", "content": CRITERIA_CRITIC_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    invocation = CriticInvocation(
        iteration=iteration,
        prompt_messages=copy.deepcopy(messages),
    )

    msg = await llm_chat(client, messages, temperature=0.0)
    invocation.response_raw = msg.content or ""

    text = invocation.response_raw
    if "```" in text:
        for p in text.split("```"):
            stripped = p.strip()
            if stripped.startswith("json"):
                text = stripped[4:].strip()
                break
            if stripped.startswith("{"):
                text = stripped
                break

    try:
        data = json.loads(text)
        invocation.verdict = data.get("verdict", "approved").lower()
        invocation.gaps = data.get("gaps", [])
        if invocation.verdict not in ("approved", "rejected"):
            invocation.verdict = "approved"
    except json.JSONDecodeError:
        invocation.verdict = "approved"

    invocation.duration_ms = (time.time() - t0) * 1000
    return invocation


# --- V4 condition runner ---

async def run_condition_v4(
    client: AsyncOpenAI,
    goal: str,
    available_files: list[str],
    scenario_dir: Path,
) -> tuple[RunTrace, CriteriaGeneration]:
    """Run agent loop with criteria-aware Critic.

    Agent receives goal + criteria in the initial user message.
    Critic at complete_goal verifies each criterion.
    """
    # Step 1: Generate criteria
    criteria_gen = await generate_criteria(client, goal, available_files)
    criteria = criteria_gen.criteria

    if not criteria:
        # Fall back gracefully
        criteria = ["The task as described should be addressed."]

    # Step 2: Build augmented goal for the agent
    criteria_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))
    augmented_goal = (
        f"{goal}\n\n"
        f"## ACCEPTANCE CRITERIA (your work will be reviewed against these)\n"
        f"{criteria_text}\n\n"
        "Your fix must satisfy ALL criteria. Investigate the codebase as needed."
    )

    # Step 3: Run agent loop with criteria-aware Critic
    t_start = time.time()
    trace = RunTrace(condition="with_criteria", scenario=scenario_dir.name)
    workspace = Workspace.from_dir(scenario_dir, available_files)

    messages: list[dict] = [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": augmented_goal},
    ]
    critic_rejections = 0

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
                if critic_rejections < _MAX_CRITIC_REJECTIONS:
                    critic = await run_criteria_critic(
                        client, goal, criteria, workspace,
                        available_files, completion_recap, iteration,
                    )
                    trace.critic_invocations.append(critic)

                    if critic.verdict == "rejected":
                        critic_rejections += 1
                        gaps_text = "\n".join(f"- {g}" for g in critic.gaps)
                        tool_result_content = (
                            f"complete_goal BLOCKED by reviewer. The following "
                            f"acceptance criteria are NOT met:\n{gaps_text}\n\n"
                            "Address each gap, then call complete_goal again."
                        )
                        iter_trace.tool_results.append({
                            "tool_call_id": tc.id, "name": name,
                            "content": tool_result_content, "critic_verdict": "rejected",
                        })
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_result_content})
                        continue
                    iter_trace.tool_results.append({
                        "tool_call_id": tc.id, "name": name,
                        "content": "complete_goal approved.", "critic_verdict": "approved",
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
    return trace, criteria_gen


# --- Main ---

async def main() -> None:
    api_key = _load_api_key()
    client = AsyncOpenAI(api_key=api_key, base_url=_API_BASE)

    scenario_dir = _SCENARIOS_DIR / "scenario_2"
    available_files = ["invoice.py", "tax_rules.py", "discounts.py", "test_invoice.py"]
    goal = (
        "Task: generate_invoice() in invoice.py uses a hardcoded 10% tax rate. "
        "Update it to use the correct regional tax rate based on order['region']. "
        "The billing system has these files in the workspace: invoice.py, tax_rules.py, "
        "discounts.py, test_invoice.py. Investigate the codebase and produce a complete fix."
    )

    print(f"V4 MULTI-TRIAL ({N_TRIALS} trials, scenario_2, temp=0)")
    print(f"Model: {_MODEL}\n")

    # Generate criteria once (deterministic at temp=0)
    print("=== GENERATING ACCEPTANCE CRITERIA ===")
    criteria_gen = await generate_criteria(client, goal, available_files)
    print(f"Generated {len(criteria_gen.criteria)} criteria in {criteria_gen.duration_ms:.0f}ms:")
    for i, c in enumerate(criteria_gen.criteria, 1):
        print(f"  {i}. {c}")

    all_baseline: list[RunTrace] = []
    all_v4: list[RunTrace] = []
    v4_criteria_per_trial: list[list[str]] = []

    for trial in range(1, N_TRIALS + 1):
        print(f"\n=== TRIAL {trial}/{N_TRIALS} ===")

        # Baseline (same as V3 baseline)
        b = await run_condition(client, goal, available_files, scenario_dir, "baseline")
        await judge_run(client, b, goal)
        all_baseline.append(b)
        print(f"  baseline:     {b.judge_score}/5  iters={len(b.iterations)}")

        # V4: criteria + critic
        v4, used_criteria_gen = await run_condition_v4(client, goal, available_files, scenario_dir)
        await judge_run(client, v4, goal)
        all_v4.append(v4)
        v4_criteria_per_trial.append(used_criteria_gen.criteria)
        verdicts = [ci.verdict for ci in v4.critic_invocations]
        rejections = sum(1 for ci in v4.critic_invocations if ci.verdict == "rejected")
        print(f"  with_criteria: {v4.judge_score}/5  iters={len(v4.iterations)}  critic={verdicts}  rejections={rejections}")

    # Aggregate
    print(f"\n{'='*60}")
    print("AGGREGATE RESULTS")
    print(f"{'='*60}")

    for cond_name, runs in (("baseline", all_baseline), ("with_criteria", all_v4)):
        scores = [r.judge_score for r in runs]
        avg = statistics.mean(scores)
        try:
            sd = statistics.stdev(scores)
        except statistics.StatisticsError:
            sd = 0.0
        print(f"\n  {cond_name}:")
        print(f"    scores: {scores}")
        print(f"    avg: {avg:.2f}  stdev: {sd:.2f}")
        print(f"    range: {max(scores) - min(scores)}")

    total_rejections = sum(
        1 for r in all_v4
        for ci in r.critic_invocations
        if ci.verdict == "rejected"
    )
    print(f"\n  Total critic rejections (V4): {total_rejections}")

    baseline_avg = statistics.mean(r.judge_score for r in all_baseline)
    v4_avg = statistics.mean(r.judge_score for r in all_v4)
    delta = v4_avg - baseline_avg
    print(f"\n  Delta (with_criteria - baseline): {delta:+.2f}")

    if delta >= 1.0:
        print("\n  VERDICT: Criteria + Critic provides MEANINGFUL improvement.")
    elif delta > 0:
        print(f"\n  VERDICT: Slight improvement ({delta:+.2f}), check if outside variability.")
    elif delta == 0:
        print("\n  VERDICT: No improvement.")
    else:
        print(f"\n  VERDICT: Criteria + Critic HURTS ({delta:.2f}).")

    output_path = _SCENARIOS_DIR / "results_v4_criteria.json"
    with open(output_path, "w") as f:
        json.dump({
            "n_trials": N_TRIALS,
            "initial_criteria": criteria_gen.criteria,
            "initial_criteria_raw": criteria_gen.response_raw,
            "baseline_traces": [serialize_trace(t) for t in all_baseline],
            "with_criteria_traces": [serialize_trace(t) for t in all_v4],
            "criteria_per_v4_trial": v4_criteria_per_trial,
        }, f, indent=2)
    print(f"\nFull traces: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
