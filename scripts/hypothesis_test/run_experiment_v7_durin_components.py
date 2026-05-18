"""V7: Test Durin's REAL components (PlanHook, plan tools, forced verify gate).

Previous experiments tested generic ReAct loops. This one wires up Durin's
actual PlanHook and plan tools, with real disk operations and real pytest
execution, to measure whether Durin's plan-tier machinery changes behavior.

Conditions:
  A) baseline: minimal agent loop (no Durin hooks, complete_goal always succeeds)
  B) with_planhook: PlanHook attached + plan tools registered + complete_goal
     gate enforced + phase-aware temperatures + escalation on verify fail

Captures per run:
  - Iterations (total + breakdown of tool calls per iter)
  - Token usage (input, output, total — cumulative)
  - Tool call breakdown by name
  - All system messages injected by hooks (including phase prompts, temperature)
  - PlanHook state trajectory (tier, phase, cycle_count, edit_detected, verify_passed)
  - complete_goal gate hits (blocked / allowed)
  - Final pytest exit code + output
  - Judge score against ground truth
  - All assistant messages (prompts and reasoning)

Each scenario runs in its own temp directory with the files copied — the
agent's edits are real, and exec("pytest") actually runs.
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

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # find durin package
sys.path.insert(0, str(Path(__file__).parent))

from openai import AsyncOpenAI

# Durin imports
from durin.agent.hook import AgentHookContext
from durin.plan.hook import PlanHook
from durin.plan.types import ExecutionTier, Phase
from durin.agent.tools.plan import set_plan_hook
from durin.providers.base import ToolCallRequest as DurinToolCallRequest

from run_experiment_v3_critic import (  # type: ignore
    _MODEL,
    _SCENARIOS_DIR,
    _load_api_key,
)

N_TRIALS = 2
_API_BASE = "https://api.z.ai/api/coding/paas/v4"
_MAX_ITERATIONS = 20


SCENARIOS = [
    {
        "name": "scenario_2_invoice_tax",
        "dir": "scenario_2",
        "files": ["invoice.py", "tax_rules.py", "discounts.py", "test_invoice.py"],
        "test_file": "test_invoice.py",
        "goal": (
            "Task: generate_invoice() in invoice.py uses a hardcoded 10% tax rate. "
            "Update it to use the correct regional tax rate based on order['region']. "
            "The billing system has these files in the workspace: invoice.py, tax_rules.py, "
            "discounts.py, test_invoice.py. Investigate, implement, and verify your fix "
            "by running the tests."
        ),
        "ground_truth": (
            "Complete fix: (1) Use get_tax_rate() from tax_rules.py. "
            "(2) Apply discounts BEFORE tax using apply_discount() from discounts.py. "
            "(3) Process discount_code from order dict. (4) Handle exemptions optionally. "
            "Score 5 = all four (or first three at minimum). Score 3 = tax rate only. "
            "Score 1-2 = no fix or wrong file."
        ),
    },
    {
        "name": "scenario_3_root_cause",
        "dir": "scenario_3",
        "files": ["prices.py", "product_db.py", "admin.py", "test_pricing.py"],
        "test_file": "test_pricing.py",
        "goal": (
            "Bug: get_price() in prices.py returns negative or zero values for some "
            "products. The discount_multiplier in the database is sometimes negative. "
            "We need get_price() to never return negative values. The pricing system "
            "has these files: prices.py, product_db.py, admin.py, test_pricing.py. "
            "Investigate the root cause, fix it, and verify with the tests."
        ),
        "ground_truth": (
            "ROOT CAUSE: admin.set_discount() does not validate discount_pct range. "
            "Score 5 = fixes admin.set_discount validation (root cause). "
            "Score 3 = only clamps in prices.py without fixing root cause. "
            "Score 1-2 = no fix or wrong approach."
        ),
    },
    {
        "name": "scenario_4_implicit_security",
        "dir": "scenario_4",
        "files": ["api.py", "auth.py", "models.py", "test_api.py"],
        "test_file": "test_api.py",
        "goal": (
            "Task: Add a `DELETE /users/{id}` endpoint to api.py. The endpoint should "
            "delete the user record identified by the path parameter. The user system has "
            "these files: api.py, auth.py, models.py, test_api.py. Implement and verify."
        ),
        "ground_truth": (
            "Complete delete endpoint: (1) check_authenticated → 401 if not. "
            "(2) check_owner_or_admin → 403 if not owner/admin. "
            "(3) delete_user_record + appropriate status. "
            "Score 5 = both auth and ownership checks. Score 3 = auth only. "
            "Score 1-2 = no auth checks."
        ),
    },
]


# --- Workspace (real disk) ---

@dataclass
class DiskWorkspace:
    root: Path
    files_tracked: list[str] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)

    @classmethod
    def from_scenario(cls, scenario: dict) -> "DiskWorkspace":
        src_dir = _SCENARIOS_DIR / scenario["dir"]
        tmp = Path(tempfile.mkdtemp(prefix=f"durin_v7_{scenario['name']}_"))
        for f in scenario["files"]:
            src = src_dir / f
            if src.exists():
                shutil.copy(src, tmp / f)
        return cls(root=tmp, files_tracked=list(scenario["files"]))

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    def list_files(self) -> list[str]:
        return sorted([p.name for p in self.root.iterdir() if p.is_file()])

    def read_file(self, path: str) -> str:
        p = self.root / path
        if not p.exists():
            return f"ERROR: File '{path}' not found. Available: {self.list_files()}"
        return p.read_text()

    def edit_file(self, path: str, content: str) -> str:
        p = self.root / path
        prev_len = len(p.read_text()) if p.exists() else 0
        p.write_text(content)
        self.edits.append({
            "path": path, "previous_length": prev_len,
            "new_length": len(content), "timestamp": time.time(),
        })
        return f"File '{path}' updated ({len(content)} bytes)"

    def exec_pytest(self, test_file: str | None = None) -> tuple[int, str]:
        """Run pytest in the workspace. Returns (exit_code, output)."""
        cmd = ["python", "-m", "pytest", "-xvs", "--tb=short"]
        if test_file:
            cmd.append(test_file)
        try:
            result = subprocess.run(
                cmd,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            return result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return -1, "pytest timed out after 30s"


# --- Tool definitions (OpenAI function calling) ---

def build_tools(include_plan: bool) -> list[dict]:
    base = [
        {"type": "function", "function": {
            "name": "list_files",
            "description": "List all files in the workspace.",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Read a file's contents.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Filename (e.g. 'invoice.py')"},
            }, "required": ["path"]},
        }},
        {"type": "function", "function": {
            "name": "edit_file",
            "description": "Replace the entire contents of a file.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            }, "required": ["path", "content"]},
        }},
        {"type": "function", "function": {
            "name": "exec",
            "description": "Run a shell command in the workspace. Use this to run tests with 'pytest'.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string", "description": "Shell command (e.g. 'pytest -xvs test_invoice.py')"},
            }, "required": ["command"]},
        }},
        {"type": "function", "function": {
            "name": "complete_goal",
            "description": "Mark the task complete with a recap.",
            "parameters": {"type": "object", "properties": {
                "recap": {"type": "string"},
            }, "required": ["recap"]},
        }},
    ]
    if include_plan:
        base.extend([
            {"type": "function", "function": {
                "name": "set_execution_mode",
                "description": (
                    "Declare execution mode. 'direct' for trivial tasks. 'plan' for "
                    "tasks that edit code (will start with EXECUTE → VERIFY fast-path; "
                    "if verify fails, escalates to full INVESTIGATE → PLAN → EXECUTE → VERIFY)."
                ),
                "parameters": {"type": "object", "properties": {
                    "tier": {"type": "string", "enum": ["direct", "plan"]},
                    "reason": {"type": "string"},
                }, "required": ["tier"]},
            }},
            {"type": "function", "function": {
                "name": "update_plan",
                "description": (
                    "Update the execution plan. Use 'add' to add steps, 'complete' to "
                    "mark done, 'fail' to mark failed. Only available in plan mode."
                ),
                "parameters": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": ["add", "complete", "fail"]},
                    "item": {"type": "string"},
                }, "required": ["action", "item"]},
            }},
        ])
    return base


AGENT_SYSTEM_BASELINE = """\
You are a software engineer. Tools: list_files, read_file, edit_file, exec, complete_goal.

WORKFLOW:
1. Explore the codebase to understand the task.
2. Read all relevant files.
3. Make changes via edit_file.
4. Run tests with exec("pytest -xvs <test_file>") to verify your fix.
5. Call complete_goal when done.

Be thorough. A complete fix may require coordinating multiple files."""

AGENT_SYSTEM_PLAN = """\
You are a software engineer. Tools: set_execution_mode, list_files, read_file, edit_file, exec, update_plan, complete_goal.

WORKFLOW:
1. FIRST: call set_execution_mode to declare 'direct' (trivial tasks) or 'plan' (anything that edits code).
2. Explore the codebase.
3. Make changes via edit_file.
4. Run tests with exec("pytest -xvs <test_file>") — required to pass before complete_goal in plan mode.
5. If tests fail, the system will escalate to a full investigation cycle.
6. Call complete_goal when done."""


# --- Comprehensive trace ---

@dataclass
class IterDetail:
    iteration: int
    phase_before: str | None = None  # PlanHook phase before this iter
    tier_before: str | None = None
    cycle_count_before: int = 0
    temperature_used: float = 0.0
    injected_system_messages: list[str] = field(default_factory=list)  # contents injected by hooks
    assistant_content: str | None = None
    assistant_reasoning: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    tokens_input: int = 0
    tokens_output: int = 0
    duration_ms: float = 0.0
    error: str | None = None


@dataclass
class RunDetail:
    scenario: str
    condition: str
    iterations: list[IterDetail] = field(default_factory=list)
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    total_duration_ms: float = 0.0
    pytest_runs: list[dict] = field(default_factory=list)  # each exec("pytest...") run
    pytest_final_exit_code: int | None = None
    pytest_final_output: str = ""
    plan_state_final: dict | None = None
    complete_goal_attempts: int = 0
    complete_goal_blocked_count: int = 0
    completed: bool = False
    completion_recap: str = ""
    stop_reason: str = ""
    judge_score: int = 0
    judge_reasoning: str = ""
    final_files: dict[str, str] = field(default_factory=dict)


# --- Agent loop ---

async def llm_chat_with_usage(
    client: AsyncOpenAI,
    messages: list[dict],
    tools: list[dict],
    temperature: float,
) -> tuple[Any, dict]:
    """Make LLM call, return (message, usage_dict). Retries on transient errors."""
    from openai import APIConnectionError, APIStatusError, InternalServerError, RateLimitError

    last_err: Exception | None = None
    for attempt in range(4):
        try:
            resp = await client.chat.completions.create(
                model=_MODEL,
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=2048,
            )
            usage = {}
            if resp.usage:
                usage = {
                    "prompt_tokens": resp.usage.prompt_tokens,
                    "completion_tokens": resp.usage.completion_tokens,
                    "total_tokens": resp.usage.total_tokens,
                }
            return resp.choices[0].message, usage
        except (InternalServerError, APIConnectionError, RateLimitError) as e:
            last_err = e
            await asyncio.sleep(2 ** attempt * 5)
        except APIStatusError as e:
            if e.status_code in (502, 503, 504):
                last_err = e
                await asyncio.sleep(2 ** attempt * 5)
            else:
                raise
    assert last_err is not None
    raise last_err


async def run_one(
    client: AsyncOpenAI,
    scenario: dict,
    condition: str,
) -> RunDetail:
    """Run one trial of one scenario in one condition."""
    detail = RunDetail(scenario=scenario["name"], condition=condition)
    workspace = DiskWorkspace.from_scenario(scenario)
    t_start = time.time()

    # Setup hooks
    plan_hook: PlanHook | None = None
    if condition == "with_planhook":
        plan_hook = PlanHook(workspace=workspace.root, session_key="v7test")
        set_plan_hook(plan_hook)
        system_prompt = AGENT_SYSTEM_PLAN
    else:
        # Clear any global plan_hook from previous runs
        set_plan_hook(None)
        system_prompt = AGENT_SYSTEM_BASELINE

    tools = build_tools(include_plan=(condition == "with_planhook"))

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": scenario["goal"]},
    ]

    try:
        for iteration in range(1, _MAX_ITERATIONS + 1):
            iter_detail = IterDetail(iteration=iteration)

            # Snapshot plan state BEFORE iteration
            if plan_hook:
                iter_detail.phase_before = (
                    plan_hook.state.current_phase.value if plan_hook.state.current_phase else None
                )
                iter_detail.tier_before = plan_hook.state.tier.value
                iter_detail.cycle_count_before = plan_hook.state.cycle_count

            # Run before_iteration hook (may inject messages, set temperature)
            msg_count_before_hooks = len(messages)
            temperature = 0.0  # default

            if plan_hook:
                ctx = AgentHookContext(
                    iteration=iteration,
                    messages=messages,
                    tool_calls=[],
                    tool_results=[],
                )
                await plan_hook.before_iteration(ctx)
                if ctx.temperature_override is not None:
                    temperature = ctx.temperature_override
                # Capture what hook injected (new messages added before the user message)
                for new_idx in range(msg_count_before_hooks, len(messages)):
                    m = messages[new_idx]
                    if m.get("role") == "system":
                        iter_detail.injected_system_messages.append(m.get("content", ""))

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
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_dict)

            if not msg.tool_calls:
                detail.stop_reason = "assistant_stopped_no_tools"
                detail.iterations.append(iter_detail)
                break

            # Execute tool calls
            completed = False
            durin_tool_calls: list[DurinToolCallRequest] = []  # for hook
            tool_results_for_hook: list[Any] = []
            had_error_in_tools = False

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}

                iter_detail.tool_calls.append({"name": name, "arguments": args, "id": tc.id})
                durin_tool_calls.append(DurinToolCallRequest(id=tc.id, name=name, arguments=args))

                # Execute
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
                        # Use the scenario's test file
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
                        # Generic shell exec — just simulate
                        result_content = "Generic exec not supported in this experiment."
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
                            result_content = "update_plan only available in plan mode. Call set_execution_mode('plan') first."
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
                    had_error_in_tools = True

                iter_detail.tool_results.append({
                    "name": name, "id": tc.id, "content": result_content[:2000],
                    "had_error": tool_error,
                })
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_content})
                tool_results_for_hook.append({"name": name, "content": result_content, "error": tool_error})

            # Run after_iteration hook (may detect verify pass/fail, set phase, emit stimuli)
            if plan_hook:
                ctx = AgentHookContext(
                    iteration=iteration,
                    messages=messages,
                    tool_calls=durin_tool_calls,
                    tool_results=tool_results_for_hook,
                    error="pytest failed" if had_error_in_tools else None,
                )
                await plan_hook.after_iteration(ctx)

            detail.iterations.append(iter_detail)

            if completed:
                detail.completed = True
                detail.stop_reason = "complete_goal"
                break

        else:
            detail.stop_reason = "max_iterations"

        # Final plan state
        if plan_hook:
            detail.plan_state_final = {
                "tier": plan_hook.state.tier.value,
                "current_phase": (
                    plan_hook.state.current_phase.value if plan_hook.state.current_phase else None
                ),
                "cycle_count": plan_hook.state.cycle_count,
                "edit_detected": plan_hook.state.edit_detected,
                "verify_passed": plan_hook.state.verify_passed,
                "items_count": len(plan_hook.state.items),
                "items": [{"description": i.description, "status": i.status} for i in plan_hook.state.items],
            }

        # Snapshot final files
        for f in workspace.files_tracked:
            try:
                detail.final_files[f] = workspace.read_file(f)
            except Exception:
                pass

    finally:
        workspace.cleanup()

    detail.total_duration_ms = (time.time() - t_start) * 1000
    return detail


# --- Judge ---

async def judge_run(client: AsyncOpenAI, detail: RunDetail, scenario: dict) -> None:
    edited_paths = sorted({
        tc["arguments"].get("path", "")
        for it in detail.iterations
        for tc in it.tool_calls
        if tc["name"] == "edit_file"
    } - {""})

    code_block = "\n\n".join(
        f"### {p} (FINAL)\n```python\n{detail.final_files.get(p, '(missing)')}\n```"
        for p in edited_paths
    ) or "(no files edited)"

    judge_prompt = (
        f"## ORIGINAL TASK\n{scenario['goal']}\n\n"
        f"## GROUND TRUTH\n{scenario['ground_truth']}\n\n"
        f"## FINAL CODE\n{code_block}\n\n"
        f"## RECAP\n{detail.completion_recap or '(no recap — did not complete)'}\n\n"
        f"## PYTEST FINAL EXIT CODE\n{detail.pytest_final_exit_code}\n\n"
        "Rate the fix 1-5. Output STRICT JSON: "
        '{"score": N, "reasoning": "one sentence"}'
    )

    msg, _ = await llm_chat_with_usage(client, [
        {"role": "system", "content": "Score the bug fix. JSON only."},
        {"role": "user", "content": judge_prompt},
    ], tools=[], temperature=0.1)

    text = msg.content or ""
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
        detail.judge_score = int(data.get("score", 0))
        detail.judge_reasoning = data.get("reasoning", "")
    except json.JSONDecodeError:
        detail.judge_reasoning = f"(parse error) {text[:200]}"


# --- Main ---

def summarize_run(d: RunDetail) -> str:
    tool_counts: dict[str, int] = {}
    for it in d.iterations:
        for tc in it.tool_calls:
            tool_counts[tc["name"]] = tool_counts.get(tc["name"], 0) + 1
    return (
        f"score={d.judge_score}/5 iters={len(d.iterations)} "
        f"tools={dict(sorted(tool_counts.items()))} "
        f"tokens={d.total_tokens_input}/{d.total_tokens_output} "
        f"pytest_exit={d.pytest_final_exit_code} "
        f"cg_blocked={d.complete_goal_blocked_count} "
        f"plan_state={d.plan_state_final}"
    )


async def main() -> None:
    api_key = _load_api_key()
    client = AsyncOpenAI(api_key=api_key, base_url=_API_BASE)

    print(f"V7 DURIN COMPONENTS ({N_TRIALS} trials × 2 conditions × {len(SCENARIOS)} scenarios)")
    print(f"Model: {_MODEL}\n")

    all_results: list[RunDetail] = []

    for scenario in SCENARIOS:
        print(f"\n{'='*70}\nSCENARIO: {scenario['name']}\n{'='*70}")
        for cond in ("baseline", "with_planhook"):
            for trial in range(1, N_TRIALS + 1):
                d = await run_one(client, scenario, cond)
                await judge_run(client, d, scenario)
                all_results.append(d)
                print(f"\n  [{cond} trial {trial}] {summarize_run(d)}")

    # Aggregate
    print(f"\n{'='*70}\nAGGREGATE\n{'='*70}\n")
    print(f"{'Scenario':<32} {'Condition':<18} {'Avg Score':<12} {'Avg Iters':<12} {'Avg Tokens':<12}")
    print("-" * 90)
    for scenario in SCENARIOS:
        for cond in ("baseline", "with_planhook"):
            results = [r for r in all_results if r.scenario == scenario["name"] and r.condition == cond]
            scores = [r.judge_score for r in results]
            iters = [len(r.iterations) for r in results]
            tokens = [r.total_tokens_input + r.total_tokens_output for r in results]
            print(
                f"{scenario['name']:<32} {cond:<18} "
                f"{statistics.mean(scores):<12.2f} {statistics.mean(iters):<12.1f} "
                f"{statistics.mean(tokens):<12.0f}"
            )

    # Save all detail
    output_path = _SCENARIOS_DIR / "results_v7_durin_components.json"
    with open(output_path, "w") as f:
        json.dump([
            {
                "scenario": r.scenario,
                "condition": r.condition,
                "judge_score": r.judge_score,
                "judge_reasoning": r.judge_reasoning,
                "completed": r.completed,
                "stop_reason": r.stop_reason,
                "completion_recap": r.completion_recap,
                "total_tokens_input": r.total_tokens_input,
                "total_tokens_output": r.total_tokens_output,
                "total_duration_ms": r.total_duration_ms,
                "complete_goal_attempts": r.complete_goal_attempts,
                "complete_goal_blocked_count": r.complete_goal_blocked_count,
                "pytest_runs_count": len(r.pytest_runs),
                "pytest_final_exit_code": r.pytest_final_exit_code,
                "pytest_runs": r.pytest_runs,
                "plan_state_final": r.plan_state_final,
                "iterations": [
                    {
                        "iter": it.iteration,
                        "phase_before": it.phase_before,
                        "tier_before": it.tier_before,
                        "cycle_count_before": it.cycle_count_before,
                        "temperature_used": it.temperature_used,
                        "injected_system_messages": it.injected_system_messages,
                        "assistant_content": it.assistant_content,
                        "tool_calls": it.tool_calls,
                        "tool_results": it.tool_results,
                        "tokens_input": it.tokens_input,
                        "tokens_output": it.tokens_output,
                        "duration_ms": it.duration_ms,
                    }
                    for it in r.iterations
                ],
                "final_files": r.final_files,
            }
            for r in all_results
        ], f, indent=2)
    print(f"\nFull traces: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
