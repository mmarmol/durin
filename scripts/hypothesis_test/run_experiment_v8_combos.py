"""V8: Test all combinations of Durin's hook stack.

Conditions (5):
  - baseline:         no hooks
  - posture_only:     PostureHook only (phrase injected in system prompt, vector tracks events)
  - plan_only:        PlanHook only (V7 condition replicated)
  - plan_posture:     PlanHook + PostureHook (PlanHook temperature includes posture modulation)
  - full_stack:       PlanHook + PostureHook + DeliberationService (everything Durin ships)

Captures the same detail as V7 plus: posture vector trajectory, deliberation invocations.

Scenarios: 3 (same as V7), 2 trials each. Total: 30 runs.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from openai import AsyncOpenAI

from durin.agent.hook import AgentHookContext
from durin.plan.hook import PlanHook
from durin.plan.types import ExecutionTier, Phase
from durin.agent.tools.plan import set_plan_hook
from durin.posture.hook import PostureHook
from durin.posture.vector import PostureVector
from durin.providers.base import ToolCallRequest as DurinToolCallRequest

from run_experiment_v7_durin_components import (  # type: ignore
    SCENARIOS,
    DiskWorkspace,
    IterDetail,
    RunDetail,
    build_tools,
    AGENT_SYSTEM_BASELINE,
    AGENT_SYSTEM_PLAN,
    llm_chat_with_usage,
    judge_run,
    summarize_run,
    _MODEL,
    _SCENARIOS_DIR,
    _API_BASE,
    _MAX_ITERATIONS,
    _load_api_key,
)

N_TRIALS = 2
CONDITIONS = ["baseline", "posture_only", "plan_only", "plan_posture", "full_stack"]


# --- Extended RunDetail for V8 ---

@dataclass
class V8Detail(RunDetail):
    posture_initial: dict[str, float] | None = None
    posture_final: dict[str, float] | None = None
    posture_phrase_initial: str = ""
    deliberation_invocations: list[dict] = field(default_factory=list)
    posture_events_per_iter: list[list[str]] = field(default_factory=list)


# --- Run one trial ---

async def run_one_v8(
    client: AsyncOpenAI,
    scenario: dict,
    condition: str,
    api_key: str,
) -> V8Detail:
    detail = V8Detail(scenario=scenario["name"], condition=condition)
    workspace = DiskWorkspace.from_scenario(scenario)
    t_start = time.time()

    # --- Setup hooks ---
    posture_hook: PostureHook | None = None
    plan_hook: PlanHook | None = None
    deliberation = None
    durin_provider = None

    if condition in ("posture_only", "plan_posture", "full_stack"):
        vector = PostureVector.default()
        posture_hook = PostureHook(vector)
        # Apply goal bias by simulating iter=0 before_iteration
        bootstrap_ctx = AgentHookContext(
            iteration=0,
            messages=[
                {"role": "system", "content": "agent system"},
                {"role": "user", "content": scenario["goal"]},
            ],
            tool_calls=[],
            tool_results=[],
        )
        await posture_hook.before_iteration(bootstrap_ctx)
        detail.posture_initial = {k.value: round(v, 4) for k, v in posture_hook.current_vector.snapshot().items()}
        detail.posture_phrase_initial = posture_hook.current_phrase

    if condition in ("plan_only", "plan_posture", "full_stack"):
        posture_fn = None
        if posture_hook:
            posture_fn = lambda: {k.value: v for k, v in posture_hook.current_vector.snapshot().items()}
        plan_hook = PlanHook(
            workspace=workspace.root,
            session_key=f"v8_{condition}",
            posture_snapshot_fn=posture_fn,
        )
        set_plan_hook(plan_hook)
    else:
        set_plan_hook(None)

    if condition == "full_stack":
        try:
            from durin.providers.openai_compat_provider import OpenAICompatProvider
            from durin.deliberation.engine import DeliberationEngine
            from durin.deliberation.service import DeliberationService

            durin_provider = OpenAICompatProvider(
                api_key=api_key,
                api_base=_API_BASE,
                default_model=_MODEL,
            )
            engine = DeliberationEngine(provider=durin_provider, model=_MODEL)
            deliberation = DeliberationService(engine)
            if plan_hook:
                plan_hook._deliberation = deliberation
        except Exception as e:
            print(f"  WARNING: deliberation setup failed: {e}")

    # --- Build system prompt ---
    if plan_hook:
        system_prompt = AGENT_SYSTEM_PLAN
    else:
        system_prompt = AGENT_SYSTEM_BASELINE
    if detail.posture_phrase_initial:
        system_prompt = f"{system_prompt}\n\n# Posture\n\n{detail.posture_phrase_initial}"

    tools = build_tools(include_plan=(plan_hook is not None))
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": scenario["goal"]},
    ]

    try:
        for iteration in range(1, _MAX_ITERATIONS + 1):
            iter_detail = IterDetail(iteration=iteration)

            if plan_hook:
                iter_detail.phase_before = (
                    plan_hook.state.current_phase.value if plan_hook.state.current_phase else None
                )
                iter_detail.tier_before = plan_hook.state.tier.value
                iter_detail.cycle_count_before = plan_hook.state.cycle_count

            # before_iteration on both hooks
            temperature = 0.0
            msg_count_before = len(messages)

            if plan_hook:
                ctx = AgentHookContext(
                    iteration=iteration, messages=messages,
                    tool_calls=[], tool_results=[],
                )
                await plan_hook.before_iteration(ctx)
                if ctx.temperature_override is not None:
                    temperature = ctx.temperature_override

            # Capture any injected system messages
            for i in range(msg_count_before, len(messages)):
                m = messages[i]
                if m.get("role") == "system":
                    iter_detail.injected_system_messages.append(m.get("content", "")[:500])
            # Also check if inserted in middle
            if len(messages) > msg_count_before:
                # Find new system messages by scanning all (since insert is in middle)
                pass  # we already captured by length diff above

            iter_detail.temperature_used = temperature

            t_iter = time.time()
            try:
                msg, usage = await llm_chat_with_usage(client, messages, tools, temperature)
            except Exception as e:
                iter_detail.error = str(e)
                detail.iterations.append(iter_detail)
                detail.stop_reason = f"llm_error: {e}"
                break

            iter_detail.duration_ms = (time.time() - t_iter) * 1000
            iter_detail.tokens_input = usage.get("prompt_tokens", 0)
            iter_detail.tokens_output = usage.get("completion_tokens", 0)
            detail.total_tokens_input += iter_detail.tokens_input
            detail.total_tokens_output += iter_detail.tokens_output

            iter_detail.assistant_content = msg.content
            assistant_dict = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                assistant_dict["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_dict)

            if not msg.tool_calls:
                detail.stop_reason = "assistant_stopped_no_tools"
                detail.iterations.append(iter_detail)
                break

            completed = False
            durin_tool_calls: list[DurinToolCallRequest] = []
            tool_results_for_hook: list[Any] = []
            had_error = False

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}

                iter_detail.tool_calls.append({"name": name, "arguments": args, "id": tc.id})
                durin_tool_calls.append(DurinToolCallRequest(id=tc.id, name=name, arguments=args))

                result_content = ""
                tool_error = False

                if name == "list_files":
                    result_content = json.dumps(workspace.list_files())
                elif name == "read_file":
                    result_content = workspace.read_file(args.get("path", ""))
                elif name == "edit_file":
                    result_content = workspace.edit_file(args.get("path", ""), args.get("content", ""))
                elif name == "exec":
                    cmd = args.get("command", "")
                    if "pytest" in cmd:
                        rc, out = workspace.exec_pytest(scenario.get("test_file"))
                        result_content = f"Exit code: {rc}\n{out[:2000]}"
                        if rc != 0:
                            tool_error = True
                        detail.pytest_runs.append({
                            "iteration": iteration, "exit_code": rc, "output": out[:1500],
                        })
                        detail.pytest_final_exit_code = rc
                        detail.pytest_final_output = out[:2000]
                    else:
                        result_content = "Only pytest supported."
                elif name == "set_execution_mode":
                    if plan_hook:
                        try:
                            plan_hook.set_tier(ExecutionTier(args.get("tier", "direct")), args.get("reason", ""))
                            result_content = f"Execution mode: {args.get('tier')}"
                        except Exception as e:
                            result_content = f"ERROR: {e}"
                    else:
                        result_content = "PlanHook not active."
                elif name == "update_plan":
                    if plan_hook:
                        if plan_hook.state.tier != ExecutionTier.PLAN:
                            result_content = "update_plan only in plan mode."
                        else:
                            result_content = plan_hook.update_plan(args.get("action", ""), args.get("item", ""))
                    else:
                        result_content = "PlanHook not active."
                elif name == "complete_goal":
                    detail.complete_goal_attempts += 1
                    recap = args.get("recap", "")
                    if plan_hook:
                        allowed, reason = plan_hook.can_complete()
                        if not allowed:
                            detail.complete_goal_blocked_count += 1
                            result_content = reason
                        else:
                            result_content = "Goal completed."
                            completed = True
                            detail.completion_recap = recap
                    else:
                        result_content = "Goal completed."
                        completed = True
                        detail.completion_recap = recap
                else:
                    result_content = f"Unknown tool: {name}"

                if tool_error:
                    had_error = True

                iter_detail.tool_results.append({
                    "name": name, "id": tc.id, "content": result_content[:2000], "had_error": tool_error,
                })
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_content})
                tool_results_for_hook.append({"name": name, "content": result_content, "error": tool_error})

            # after_iteration on both hooks
            after_ctx = AgentHookContext(
                iteration=iteration, messages=messages,
                tool_calls=durin_tool_calls, tool_results=tool_results_for_hook,
                error="pytest failed" if had_error else None,
            )
            if plan_hook:
                await plan_hook.after_iteration(after_ctx)
                # Record deliberation invocations after this hook ran
                if deliberation and hasattr(deliberation, "history"):
                    n_so_far = len(detail.deliberation_invocations)
                    n_now = len(deliberation.history)
                    if n_now > n_so_far:
                        for h in deliberation.history[n_so_far:]:
                            detail.deliberation_invocations.append({
                                "iteration": iteration,
                                "trigger": h.trigger,
                                "cycle": h.cycle,
                                "duration_ms": h.duration_ms,
                                "synthesis_brief": h.synthesis_brief,
                            })

            if posture_hook:
                # Use the same context — PostureHook also reads external_stimulus_events
                # set by PlanHook earlier
                snap_before = {k.value: v for k, v in posture_hook.current_vector.snapshot().items()}
                await posture_hook.after_iteration(after_ctx)
                snap_after = {k.value: v for k, v in posture_hook.current_vector.snapshot().items()}
                events_fired = []
                if any(abs(snap_after[k] - snap_before[k]) > 0.001 for k in snap_after):
                    events_fired = sorted(after_ctx.external_stimulus_events)
                detail.posture_events_per_iter.append(events_fired)

            detail.iterations.append(iter_detail)

            if completed:
                detail.completed = True
                detail.stop_reason = "complete_goal"
                break
        else:
            detail.stop_reason = "max_iterations"

        if plan_hook:
            detail.plan_state_final = {
                "tier": plan_hook.state.tier.value,
                "current_phase": (plan_hook.state.current_phase.value if plan_hook.state.current_phase else None),
                "cycle_count": plan_hook.state.cycle_count,
                "edit_detected": plan_hook.state.edit_detected,
                "verify_passed": plan_hook.state.verify_passed,
                "items_count": len(plan_hook.state.items),
            }
        if posture_hook:
            detail.posture_final = {k.value: round(v, 4) for k, v in posture_hook.current_vector.snapshot().items()}

        for f in workspace.files_tracked:
            try:
                detail.final_files[f] = workspace.read_file(f)
            except Exception:
                pass

    finally:
        workspace.cleanup()

    detail.total_duration_ms = (time.time() - t_start) * 1000
    return detail


# --- Main ---

async def main() -> None:
    api_key = _load_api_key()
    client = AsyncOpenAI(api_key=api_key, base_url=_API_BASE)

    print(f"V8 COMBOS ({N_TRIALS} × {len(CONDITIONS)} × {len(SCENARIOS)} = {N_TRIALS*len(CONDITIONS)*len(SCENARIOS)} runs)")
    print(f"Model: {_MODEL}\n")
    print(f"Conditions: {CONDITIONS}\n")

    all_results: list[V8Detail] = []

    for scenario in SCENARIOS:
        print(f"\n{'='*70}\nSCENARIO: {scenario['name']}\n{'='*70}")
        for cond in CONDITIONS:
            for trial in range(1, N_TRIALS + 1):
                d = await run_one_v8(client, scenario, cond, api_key)
                await judge_run(client, d, scenario)
                all_results.append(d)
                extras = ""
                if d.posture_initial:
                    extras += f" posture_init={d.posture_initial}"
                if d.deliberation_invocations:
                    extras += f" delib={len(d.deliberation_invocations)}"
                print(f"  [{cond:<14} t{trial}] {summarize_run(d)}{extras}")

    # Aggregate
    print(f"\n{'='*70}\nAGGREGATE\n{'='*70}\n")
    print(f"{'Scenario':<32} {'Condition':<15} {'Score avg':<12} {'Iters avg':<12} {'Tokens avg':<12} {'Gates':<8}")
    print("-" * 100)
    for scenario in SCENARIOS:
        for cond in CONDITIONS:
            results = [r for r in all_results if r.scenario == scenario["name"] and r.condition == cond]
            scores = [r.judge_score for r in results]
            iters = [len(r.iterations) for r in results]
            tokens = [r.total_tokens_input + r.total_tokens_output for r in results]
            gates = sum(r.complete_goal_blocked_count for r in results)
            print(
                f"{scenario['name']:<32} {cond:<15} "
                f"{statistics.mean(scores):<12.2f} {statistics.mean(iters):<12.1f} "
                f"{statistics.mean(tokens):<12.0f} {gates:<8}"
            )

    # Save
    output_path = _SCENARIOS_DIR / "results_v8_combos.json"
    with open(output_path, "w") as f:
        json.dump([
            {
                "scenario": r.scenario, "condition": r.condition,
                "judge_score": r.judge_score, "judge_reasoning": r.judge_reasoning,
                "completed": r.completed, "stop_reason": r.stop_reason,
                "completion_recap": r.completion_recap,
                "total_tokens_input": r.total_tokens_input,
                "total_tokens_output": r.total_tokens_output,
                "total_duration_ms": r.total_duration_ms,
                "complete_goal_blocked_count": r.complete_goal_blocked_count,
                "pytest_final_exit_code": r.pytest_final_exit_code,
                "plan_state_final": r.plan_state_final,
                "posture_initial": r.posture_initial,
                "posture_final": r.posture_final,
                "posture_phrase_initial": r.posture_phrase_initial,
                "deliberation_invocations": r.deliberation_invocations,
                "posture_events_per_iter": r.posture_events_per_iter,
                "iterations": [
                    {"iter": it.iteration, "phase_before": it.phase_before,
                     "tier_before": it.tier_before, "temperature": it.temperature_used,
                     "tokens_in": it.tokens_input, "tokens_out": it.tokens_output,
                     "tools": [tc["name"] for tc in it.tool_calls]}
                    for it in r.iterations
                ],
                "final_files": r.final_files,
            }
            for r in all_results
        ], f, indent=2)
    print(f"\nFull traces: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
