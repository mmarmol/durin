"""Hypothesis test V3: Pre-completion adversarial review (Devin Critic pattern).

Tests whether a separate LLM call at complete_goal time, with clean context,
catches gaps that the executing agent missed (insufficient exploration,
shallow acceptance criteria).

Two conditions on scenario 2 (invoice tax integration, known failure case):
  A) BASELINE: Standard agent loop, completes when it claims done
  B) WITH_CRITIC: Same loop, but Critic intercepts complete_goal and can reject

Full observability: every prompt sent, every response, every tool call,
every Critic invocation is captured.

Usage:
    .venv/bin/python scripts/hypothesis_test/run_experiment_v3_critic.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

_ENV_PATH = Path.home() / ".hermes" / ".env"
_API_BASE = "https://api.z.ai/api/coding/paas/v4"
_MODEL = "glm-5.1"
_SCENARIOS_DIR = Path(__file__).parent
_MAX_ITERATIONS = 15
_MAX_CRITIC_REJECTIONS = 3


def _load_api_key() -> str:
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            if line.startswith("ZAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("ZAI_API_KEY", "")


# --- Tool definitions (OpenAI function calling format) ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files available in the workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Filename to read (e.g. 'invoice.py')"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace the entire contents of a file with new content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Filename to edit"},
                    "content": {"type": "string", "description": "Full new content of the file"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_goal",
            "description": "Mark the task as fully complete with a recap of what was done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recap": {"type": "string", "description": "Summary of what was done"},
                },
                "required": ["recap"],
            },
        },
    },
]


# --- Workspace simulation ---

@dataclass
class Workspace:
    """In-memory file workspace seeded from a scenario directory."""

    files: dict[str, str] = field(default_factory=dict)
    edits: list[dict] = field(default_factory=list)

    @classmethod
    def from_dir(cls, scenario_dir: Path, allowed: list[str]) -> "Workspace":
        files = {}
        for name in allowed:
            path = scenario_dir / name
            if path.exists():
                files[name] = path.read_text()
        return cls(files=files)

    def list_files(self) -> list[str]:
        return sorted(self.files.keys())

    def read_file(self, path: str) -> str:
        if path not in self.files:
            return f"ERROR: File '{path}' not found. Available: {self.list_files()}"
        return self.files[path]

    def edit_file(self, path: str, content: str) -> str:
        prev = self.files.get(path, "")
        self.files[path] = content
        self.edits.append({
            "path": path,
            "previous_length": len(prev),
            "new_length": len(content),
            "timestamp": time.time(),
        })
        return f"File '{path}' updated ({len(content)} bytes)"


# --- Trace structures ---

@dataclass
class IterationTrace:
    iteration: int
    messages_at_call: list[dict] = field(default_factory=list)
    assistant_message: dict = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    duration_ms: float = 0.0


@dataclass
class CriticInvocation:
    iteration: int
    prompt_messages: list[dict] = field(default_factory=list)
    response_raw: str = ""
    verdict: str = ""  # "approved" | "rejected"
    gaps: list[str] = field(default_factory=list)
    duration_ms: float = 0.0


@dataclass
class RunTrace:
    condition: str
    scenario: str
    iterations: list[IterationTrace] = field(default_factory=list)
    critic_invocations: list[CriticInvocation] = field(default_factory=list)
    final_edited_files: dict[str, str] = field(default_factory=dict)
    completed: bool = False
    completion_recap: str = ""
    stop_reason: str = ""
    total_duration_ms: float = 0.0
    judge_score: int = 0
    judge_reasoning: str = ""


# --- Agent loop ---

AGENT_SYSTEM = """\
You are a software engineer working on a task. You have access to these tools:
- list_files: see what files exist
- read_file: read a file's contents
- edit_file: replace a file's full contents
- complete_goal: mark the task as done with a recap

WORKFLOW:
1. Explore the codebase to understand the problem.
2. Read all files relevant to the task.
3. Make your changes via edit_file.
4. Call complete_goal when you're confident the task is fully addressed.

Be thorough. Read related files before assuming what they contain. \
A complete fix may require coordinating multiple files."""


async def llm_chat(
    client: AsyncOpenAI,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.0,
) -> Any:
    """Make a chat completion call. Retries up to 4 times on transient errors."""
    from openai import APIConnectionError, APIStatusError, InternalServerError, RateLimitError

    kwargs: dict[str, Any] = {
        "model": _MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 2048,
    }
    if tools:
        kwargs["tools"] = tools

    last_err: Exception | None = None
    for attempt in range(4):
        try:
            resp = await client.chat.completions.create(**kwargs)
            return resp.choices[0].message
        except (InternalServerError, APIConnectionError, RateLimitError) as e:
            last_err = e
            wait = 2 ** attempt * 5  # 5, 10, 20, 40s
            await asyncio.sleep(wait)
        except APIStatusError as e:
            if e.status_code in (502, 503, 504):
                last_err = e
                wait = 2 ** attempt * 5
                await asyncio.sleep(wait)
            else:
                raise
    assert last_err is not None
    raise last_err


async def execute_tool_call(
    workspace: Workspace, name: str, args: dict
) -> str:
    """Execute a tool call against the workspace."""
    if name == "list_files":
        return json.dumps(workspace.list_files())
    if name == "read_file":
        return workspace.read_file(args.get("path", ""))
    if name == "edit_file":
        return workspace.edit_file(args.get("path", ""), args.get("content", ""))
    if name == "complete_goal":
        return f"complete_goal called with recap: {args.get('recap', '')}"
    return f"ERROR: unknown tool {name}"


# --- Critic ---

CRITIC_SYSTEM = """\
You are a senior code reviewer with CLEAN CONTEXT. You will be shown:
1. The ORIGINAL TASK that was given to a junior engineer.
2. The CODE CHANGES they made (final state of edited files).
3. Their RECAP of what they did.
4. All FILES that were available in the workspace (so you can spot what they may have ignored).

Your job: judge whether the task is genuinely complete or whether something was missed.

Be skeptical. Common failure modes:
- The fix addresses the surface symptom but misses a deeper integration point.
- The engineer ignored a relevant file in the workspace.
- The fix breaks an implicit requirement that wasn't tested.
- The fix is correct in isolation but doesn't compose with other features in the codebase.

Output STRICT JSON:
{
  "verdict": "approved" or "rejected",
  "reasoning": "one paragraph",
  "gaps": ["specific missing item 1", "specific missing item 2"]
}

If verdict is "rejected", gaps MUST list concrete, actionable items the engineer should address next."""


async def run_critic(
    client: AsyncOpenAI,
    goal: str,
    workspace: Workspace,
    available_files: list[str],
    recap: str,
    iteration: int,
) -> CriticInvocation:
    """Run the Critic with clean context."""
    t0 = time.time()

    edited_contents = "\n\n".join(
        f"### {e['path']} (FINAL STATE)\n```python\n{workspace.files[e['path']]}\n```"
        for e in workspace.edits
        if e['path'] in workspace.files
    ) or "(no files were edited)"

    user_msg = (
        f"## ORIGINAL TASK\n{goal}\n\n"
        f"## FILES AVAILABLE IN WORKSPACE\n{', '.join(available_files)}\n\n"
        f"## CODE CHANGES MADE BY ENGINEER\n{edited_contents}\n\n"
        f"## ENGINEER'S RECAP\n{recap}\n\n"
        "Judge whether the task is fully complete. Output STRICT JSON only."
    )

    messages = [
        {"role": "system", "content": CRITIC_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    invocation = CriticInvocation(
        iteration=iteration,
        prompt_messages=copy.deepcopy(messages),
    )

    msg = await llm_chat(client, messages, temperature=0.1)
    invocation.response_raw = msg.content or ""

    # Parse JSON
    text = invocation.response_raw
    if "```" in text:
        parts = text.split("```")
        for p in parts:
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
        invocation.verdict = "approved"  # fail-open if can't parse

    invocation.duration_ms = (time.time() - t0) * 1000
    return invocation


# --- Run condition ---

async def run_condition(
    client: AsyncOpenAI,
    goal: str,
    available_files: list[str],
    scenario_dir: Path,
    condition: str,
) -> RunTrace:
    """Run the agent loop in either baseline or with_critic mode."""
    t_start = time.time()
    trace = RunTrace(condition=condition, scenario=scenario_dir.name)
    workspace = Workspace.from_dir(scenario_dir, available_files)

    messages: list[dict] = [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": goal},
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

        assistant_dict = {
            "role": "assistant",
            "content": msg.content,
        }
        if msg.tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]

        iter_trace.assistant_message = copy.deepcopy(assistant_dict)
        messages.append(assistant_dict)

        if not msg.tool_calls:
            # No tool calls — agent finished without complete_goal
            trace.stop_reason = "assistant_stopped_no_tools"
            trace.iterations.append(iter_trace)
            break

        # Execute each tool call
        completed_this_iter = False
        completion_recap = ""

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                args = {}

            iter_trace.tool_calls.append({
                "id": tc.id,
                "name": name,
                "arguments": args,
            })

            if name == "complete_goal":
                completion_recap = args.get("recap", "")

                if condition == "with_critic" and critic_rejections < _MAX_CRITIC_REJECTIONS:
                    # Run critic before allowing completion
                    critic = await run_critic(
                        client, goal, workspace, available_files,
                        completion_recap, iteration,
                    )
                    trace.critic_invocations.append(critic)

                    if critic.verdict == "rejected":
                        critic_rejections += 1
                        gaps_text = "\n".join(f"- {g}" for g in critic.gaps)
                        tool_result_content = (
                            f"complete_goal BLOCKED by reviewer. "
                            f"The reviewer found these gaps that must be addressed:\n{gaps_text}\n\n"
                            "Continue working on the task. Address each gap, then call complete_goal again."
                        )
                        iter_trace.tool_results.append({
                            "tool_call_id": tc.id,
                            "name": name,
                            "content": tool_result_content,
                            "critic_verdict": "rejected",
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_result_content,
                        })
                        continue
                    # Approved
                    iter_trace.tool_results.append({
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": "complete_goal approved by reviewer.",
                        "critic_verdict": "approved",
                    })

                completed_this_iter = True
                trace.completion_recap = completion_recap
                break

            result_content = await execute_tool_call(workspace, name, args)
            iter_trace.tool_results.append({
                "tool_call_id": tc.id,
                "name": name,
                "content": result_content[:3000],  # truncate huge results in trace
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_content,
            })

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


# --- Judge (against ground truth) ---

GROUND_TRUTH_SCENARIO_2 = (
    "Complete fix requires: (1) Use get_tax_rate() from tax_rules.py instead of hardcoded 0.10. "
    "(2) Handle tax-exempt items via is_tax_exempt(). "
    "(3) Apply discounts BEFORE tax using apply_discount() from discounts.py. "
    "(4) Process discount_code from order dict. "
    "A fix that only swaps the tax rate but ignores discounts and exemptions is incomplete (score 2-3 max). "
    "A fix that addresses all four points scores 5."
)


async def judge_run(client: AsyncOpenAI, trace: RunTrace, goal: str) -> None:
    """Score the final state against ground truth.

    Shows the judge ALL files that the agent edited (not just invoice.py).
    Falls back to "(no files edited)" if nothing was changed.
    """
    # Collect which files were actually edited (any path that appears in an edit_file tool call)
    edited_paths: list[str] = []
    seen: set[str] = set()
    for it in trace.iterations:
        for tc in it.tool_calls:
            if tc.get("name") == "edit_file":
                path = tc.get("arguments", {}).get("path", "")
                if path and path not in seen:
                    seen.add(path)
                    edited_paths.append(path)

    if edited_paths:
        code_block = "\n\n".join(
            f"### {p} (FINAL STATE)\n```python\n{trace.final_edited_files.get(p, '(missing)')}\n```"
            for p in edited_paths
        )
    else:
        code_block = "(no files were edited)"

    judge_prompt = (
        f"## ORIGINAL TASK\n{goal}\n\n"
        f"## GROUND TRUTH (what a complete fix requires)\n{GROUND_TRUTH_SCENARIO_2}\n\n"
        f"## FINAL CODE (all files edited by the agent)\n{code_block}\n\n"
        f"## AGENT'S RECAP\n{trace.completion_recap or '(no recap — did not complete)'}\n\n"
        "Rate the fix 1-5. Output STRICT JSON: "
        '{"score": N, "reasoning": "one sentence"}'
    )

    msg = await llm_chat(client, [
        {"role": "system", "content": "Score the bug fix against ground truth. Output JSON only."},
        {"role": "user", "content": judge_prompt},
    ], temperature=0.1)

    text = msg.content or ""
    if "```" in text:
        parts = text.split("```")
        for p in parts:
            stripped = p.strip()
            if stripped.startswith("json"):
                text = stripped[4:].strip()
                break
            if stripped.startswith("{"):
                text = stripped
                break
    try:
        data = json.loads(text)
        trace.judge_score = int(data.get("score", 0))
        trace.judge_reasoning = data.get("reasoning", "")
    except (json.JSONDecodeError, ValueError):
        trace.judge_reasoning = f"(parse error) {text[:200]}"


# --- Main ---

def serialize_trace(trace: RunTrace) -> dict:
    return {
        "condition": trace.condition,
        "scenario": trace.scenario,
        "completed": trace.completed,
        "stop_reason": trace.stop_reason,
        "completion_recap": trace.completion_recap,
        "judge_score": trace.judge_score,
        "judge_reasoning": trace.judge_reasoning,
        "total_duration_ms": trace.total_duration_ms,
        "iterations_used": len(trace.iterations),
        "critic_count": len(trace.critic_invocations),
        "edits_made": [e["path"] for it in [] for e in []] or sorted(set(
            tc["arguments"].get("path", "")
            for it in trace.iterations
            for tc in it.tool_calls
            if tc["name"] == "edit_file"
        )),
        "iterations": [
            {
                "iteration": it.iteration,
                "duration_ms": it.duration_ms,
                "messages_sent_count": len(it.messages_at_call),
                "messages_sent": it.messages_at_call,
                "assistant_response": it.assistant_message,
                "tool_calls": it.tool_calls,
                "tool_results": it.tool_results,
            }
            for it in trace.iterations
        ],
        "critic_invocations": [
            {
                "iteration": c.iteration,
                "verdict": c.verdict,
                "gaps": c.gaps,
                "response_raw": c.response_raw,
                "prompt_messages": c.prompt_messages,
                "duration_ms": c.duration_ms,
            }
            for c in trace.critic_invocations
        ],
        "final_files": trace.final_edited_files,
    }


def print_trace_summary(trace: RunTrace) -> None:
    print(f"\n  Stop reason: {trace.stop_reason}")
    print(f"  Iterations used: {len(trace.iterations)}")
    print(f"  Critic invocations: {len(trace.critic_invocations)}")
    for i, c in enumerate(trace.critic_invocations, 1):
        print(f"    Critic #{i} (iter {c.iteration}): {c.verdict.upper()}")
        for g in c.gaps:
            print(f"      gap: {g}")
    print(f"  Files edited: {sorted(set(tc['arguments'].get('path', '') for it in trace.iterations for tc in it.tool_calls if tc['name'] == 'edit_file'))}")
    print(f"  Tool calls per iter: {[len(it.tool_calls) for it in trace.iterations]}")
    print(f"  Total duration: {trace.total_duration_ms:.0f}ms")
    print(f"  Judge score: {trace.judge_score}/5 — {trace.judge_reasoning}")


async def main() -> None:
    api_key = _load_api_key()
    if not api_key:
        print("ERROR: No API key")
        return
    client = AsyncOpenAI(api_key=api_key, base_url=_API_BASE)

    # Scenario 2 (known failure case)
    scenario_dir = _SCENARIOS_DIR / "scenario_2"
    available_files = ["invoice.py", "tax_rules.py", "discounts.py", "test_invoice.py"]
    goal = (
        "Task: generate_invoice() in invoice.py uses a hardcoded 10% tax rate. "
        "Update it to use the correct regional tax rate based on order['region']. "
        "The billing system has these files in the workspace: invoice.py, tax_rules.py, "
        "discounts.py, test_invoice.py. Investigate the codebase and produce a complete fix."
    )

    print(f"{'='*70}")
    print(f"V3 EXPERIMENT: Pre-completion adversarial review (Devin Critic)")
    print(f"Scenario: {scenario_dir.name}")
    print(f"{'='*70}")

    # Condition A: BASELINE
    print(f"\n{'─'*70}")
    print("CONDITION A: BASELINE (no critic)")
    print(f"{'─'*70}")
    baseline = await run_condition(client, goal, available_files, scenario_dir, "baseline")
    await judge_run(client, baseline, goal)
    print_trace_summary(baseline)

    # Condition B: WITH_CRITIC
    print(f"\n{'─'*70}")
    print("CONDITION B: WITH_CRITIC (pre-completion review)")
    print(f"{'─'*70}")
    with_critic = await run_condition(client, goal, available_files, scenario_dir, "with_critic")
    await judge_run(client, with_critic, goal)
    print_trace_summary(with_critic)

    # Comparison
    print(f"\n{'='*70}")
    print("COMPARISON")
    print(f"{'='*70}")
    print(f"  Baseline:    {baseline.judge_score}/5 ({baseline.stop_reason}, {len(baseline.iterations)} iters)")
    print(f"  With Critic: {with_critic.judge_score}/5 ({with_critic.stop_reason}, {len(with_critic.iterations)} iters, {len(with_critic.critic_invocations)} critic calls)")
    delta = with_critic.judge_score - baseline.judge_score
    print(f"  Delta: {'+' if delta > 0 else ''}{delta}")

    # Save full traces
    output_path = _SCENARIOS_DIR / "results_v3_critic.json"
    with open(output_path, "w") as f:
        json.dump({
            "baseline": serialize_trace(baseline),
            "with_critic": serialize_trace(with_critic),
        }, f, indent=2)
    print(f"\nFull traces saved to: {output_path}")
    print(f"  Use: jq '.baseline.iterations[].tool_calls' {output_path.name}")
    print(f"  Use: jq '.with_critic.critic_invocations' {output_path.name}")


if __name__ == "__main__":
    asyncio.run(main())
