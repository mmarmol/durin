"""Hypothesis test: Does a structured challenge step improve investigation quality?

Experiment design:
  For each scenario, we run two conditions:
    A) BASELINE: LLM sees bug report + one obvious file → proposes fix
    B) CHALLENGE: Same start, then a challenge prompt asks "what are you missing?",
       LLM identifies gaps, gets additional files, then proposes fix

  We measure:
    1. Files requested after challenge (exploration coverage)
    2. Quality of final fix (graded by separate LLM call)

Usage:
    .venv/bin/python scripts/hypothesis_test/run_experiment.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI

# --- Config ---

_ENV_PATH = Path.home() / ".hermes" / ".env"
_API_BASE = "https://api.z.ai/api/coding/paas/v4"
_MODEL = "glm-5.1"
_SCENARIOS_DIR = Path(__file__).parent


def _load_api_key() -> str:
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            if line.startswith("ZAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    key = os.environ.get("ZAI_API_KEY", "")
    if not key:
        raise RuntimeError(f"No ZAI_API_KEY found in {_ENV_PATH} or environment")
    return key


# --- Data types ---

@dataclass
class ExperimentResult:
    scenario: str
    condition: str  # "baseline" or "challenge"
    files_shown: list[str]
    files_requested_after_challenge: list[str] = field(default_factory=list)
    proposed_fix: str = ""
    fix_quality_score: int = 0  # 1-5, graded by judge
    fix_quality_reasoning: str = ""
    duration_ms: float = 0.0
    raw_responses: list[str] = field(default_factory=list)


# --- File loading ---

def load_scenario_files(scenario_dir: Path) -> dict[str, str]:
    """Load all .py and .md files from a scenario directory."""
    files = {}
    for f in sorted(scenario_dir.glob("*")):
        if f.suffix in (".py", ".md"):
            files[f.name] = f.read_text()
    return files


# --- Prompts ---

SYSTEM_PROMPT = """\
You are a senior software engineer investigating a bug. \
You will be shown a bug report and source files from the project. \
Analyze the code carefully and propose a fix."""

CHALLENGE_PROMPT = """\
STOP. Before proposing a fix, critically examine your investigation:

1. WHAT FILES HAVEN'T YOU SEEN? List every file mentioned in the bug report \
or imported by the code you've read that you haven't examined yet. \
For each, explain what information it might contain that's relevant.

2. WHAT ASSUMPTIONS ARE YOU MAKING? List assumptions about how the code works \
that you haven't verified by reading the actual source.

3. WHAT COULD GO WRONG with your current understanding? What's the most likely \
way your fix could be WRONG because of something you haven't checked?

Be specific. Name files, functions, and variables."""

JUDGE_PROMPT = """\
You are evaluating a proposed bug fix. Rate the fix quality from 1-5:

5 = Correct root cause identified AND fix addresses it completely
4 = Correct root cause, fix mostly right but minor gap
3 = Partially correct — addresses a symptom but misses the real root cause
2 = Wrong approach — would not fix the bug or introduces new issues
1 = Completely off — misunderstands the problem

The GROUND TRUTH for this bug is provided below. Compare the proposed fix against it.

Respond in this exact JSON format:
{"score": N, "reasoning": "one sentence explanation"}"""


# --- Scenario definitions ---

@dataclass
class Scenario:
    name: str
    bug_report: str
    initial_files: list[str]  # Files shown first
    hidden_files: list[str]   # Files only shown after challenge
    ground_truth: str         # What the correct fix actually is
    all_files: dict[str, str] = field(default_factory=dict)


def build_scenarios() -> list[Scenario]:
    s1_dir = _SCENARIOS_DIR / "scenario_1"
    s1_files = load_scenario_files(s1_dir)

    s2_dir = _SCENARIOS_DIR / "scenario_2"
    s2_files = load_scenario_files(s2_dir)

    return [
        Scenario(
            name="notification_cache_bug",
            bug_report=(
                "Bug: Users report not receiving email notifications after updating "
                "their email address. Notifications resume after ~1 hour.\n\n"
                "The notification system has these files:\n"
                "- sender.py (send_notification entry point)\n"
                "- preferences.py (user preference lookup with caching)\n"
                "- templates.py (template rendering)\n"
                "- user_service.py (user profile management)"
            ),
            initial_files=["sender.py"],
            hidden_files=["preferences.py", "user_service.py", "templates.py"],
            ground_truth=(
                "The root cause is in user_service.py: update_user_profile() changes "
                "the email in the database but does NOT call invalidate_cache() from "
                "preferences.py. The preferences cache (1hr TTL) keeps serving the old "
                "email to send_notification(). Fix: call invalidate_cache(user_id) in "
                "update_user_profile() after updating the database."
            ),
            all_files=s1_files,
        ),
        Scenario(
            name="invoice_tax_integration",
            bug_report=(
                "Task: generate_invoice() uses a hardcoded 10% tax rate. We need "
                "regional tax rates based on order['region'].\n\n"
                "The billing system has these files:\n"
                "- invoice.py (generate_invoice function)\n"
                "- tax_rules.py (regional tax rates + exemptions)\n"
                "- discounts.py (discount code processing)\n"
                "- test_invoice.py (existing tests)"
            ),
            initial_files=["invoice.py"],
            hidden_files=["tax_rules.py", "discounts.py", "test_invoice.py"],
            ground_truth=(
                "A complete fix requires: (1) Import and use get_tax_rate() from "
                "tax_rules.py instead of hardcoded 0.10. (2) Handle tax-exempt items "
                "via is_tax_exempt(). (3) Apply discounts BEFORE tax (discounts.py says "
                "'Discounts should be applied to the subtotal BEFORE tax calculation'). "
                "(4) Process discount_code from order dict using apply_discount(). "
                "A fix that only swaps the tax rate but ignores discounts and exemptions "
                "is incomplete."
            ),
            all_files=s2_files,
        ),
    ]


# --- LLM calls ---

async def llm_call(client: AsyncOpenAI, messages: list[dict], temperature: float = 0.3) -> str:
    """Make a single LLM call and return the content."""
    resp = await client.chat.completions.create(
        model=_MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=2048,
    )
    return resp.choices[0].message.content or ""


def format_file_content(name: str, content: str) -> str:
    return f"### {name}\n```python\n{content}\n```"


# --- Experiment runners ---

async def run_baseline(
    client: AsyncOpenAI, scenario: Scenario
) -> ExperimentResult:
    """Condition A: Show bug + initial file → ask for fix directly."""
    t0 = time.time()

    file_contents = "\n\n".join(
        format_file_content(f, scenario.all_files[f])
        for f in scenario.initial_files
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"## Bug Report\n{scenario.bug_report}\n\n"
            f"## Source Code\n{file_contents}\n\n"
            "Based on what you see, propose a fix. Be specific about "
            "what code to change and why."
        )},
    ]

    fix_response = await llm_call(client, messages)

    return ExperimentResult(
        scenario=scenario.name,
        condition="baseline",
        files_shown=list(scenario.initial_files),
        proposed_fix=fix_response,
        duration_ms=(time.time() - t0) * 1000,
        raw_responses=[fix_response],
    )


async def run_challenge(
    client: AsyncOpenAI, scenario: Scenario
) -> ExperimentResult:
    """Condition B: Show bug + initial file → challenge → show more files → fix."""
    t0 = time.time()
    raw_responses = []

    file_contents = "\n\n".join(
        format_file_content(f, scenario.all_files[f])
        for f in scenario.initial_files
    )

    # Step 1: Same initial prompt
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"## Bug Report\n{scenario.bug_report}\n\n"
            f"## Source Code\n{file_contents}\n\n"
            "Before proposing a fix, first describe what you think "
            "the issue might be and what you'd want to investigate further."
        )},
    ]

    initial_analysis = await llm_call(client, messages)
    raw_responses.append(initial_analysis)

    # Step 2: Challenge prompt
    messages.append({"role": "assistant", "content": initial_analysis})
    messages.append({"role": "user", "content": CHALLENGE_PROMPT})

    challenge_response = await llm_call(client, messages)
    raw_responses.append(challenge_response)

    # Step 3: Show all remaining files (simulating the agent exploring them)
    additional_contents = "\n\n".join(
        format_file_content(f, scenario.all_files[f])
        for f in scenario.hidden_files
        if f in scenario.all_files
    )

    messages.append({"role": "assistant", "content": challenge_response})
    messages.append({"role": "user", "content": (
        "Good analysis. Here are the additional files you identified:\n\n"
        f"{additional_contents}\n\n"
        "Now, with this full picture, propose your fix. Be specific about "
        "what code to change and why."
    )})

    fix_response = await llm_call(client, messages)
    raw_responses.append(fix_response)

    # Extract which files the challenge identified
    requested = []
    for f in scenario.hidden_files:
        if f.replace(".py", "").lower() in challenge_response.lower():
            requested.append(f)

    return ExperimentResult(
        scenario=scenario.name,
        condition="challenge",
        files_shown=list(scenario.initial_files),
        files_requested_after_challenge=requested,
        proposed_fix=fix_response,
        duration_ms=(time.time() - t0) * 1000,
        raw_responses=raw_responses,
    )


async def judge_fix(
    client: AsyncOpenAI, scenario: Scenario, result: ExperimentResult
) -> None:
    """Have the LLM grade the fix quality against ground truth."""
    messages = [
        {"role": "system", "content": JUDGE_PROMPT},
        {"role": "user", "content": (
            f"## Bug Report\n{scenario.bug_report}\n\n"
            f"## Ground Truth (correct fix)\n{scenario.ground_truth}\n\n"
            f"## Proposed Fix\n{result.proposed_fix}\n\n"
            "Rate this fix (JSON format):"
        )},
    ]

    response = await llm_call(client, messages, temperature=0.1)

    try:
        # Try to extract JSON from response
        json_str = response
        if "```" in json_str:
            json_str = json_str.split("```")[1].strip()
            if json_str.startswith("json"):
                json_str = json_str[4:].strip()
        data = json.loads(json_str)
        result.fix_quality_score = int(data.get("score", 0))
        result.fix_quality_reasoning = data.get("reasoning", response)
    except (json.JSONDecodeError, ValueError):
        result.fix_quality_reasoning = f"(parse error) {response[:200]}"


# --- Main ---

async def main():
    api_key = _load_api_key()
    client = AsyncOpenAI(api_key=api_key, base_url=_API_BASE)

    scenarios = build_scenarios()
    all_results: list[ExperimentResult] = []

    for scenario in scenarios:
        print(f"\n{'='*70}")
        print(f"SCENARIO: {scenario.name}")
        print(f"{'='*70}")

        # Run baseline
        print("\n--- Condition A: BASELINE (no challenge) ---")
        baseline = await run_baseline(client, scenario)
        await judge_fix(client, scenario, baseline)
        all_results.append(baseline)

        print(f"  Files shown: {baseline.files_shown}")
        print(f"  Fix quality: {baseline.fix_quality_score}/5")
        print(f"  Reasoning: {baseline.fix_quality_reasoning}")
        print(f"  Duration: {baseline.duration_ms:.0f}ms")

        # Run challenge
        print("\n--- Condition B: CHALLENGE (with gap analysis) ---")
        challenged = await run_challenge(client, scenario)
        await judge_fix(client, scenario, challenged)
        all_results.append(challenged)

        print(f"  Files shown: {challenged.files_shown}")
        print(f"  Files identified by challenge: {challenged.files_requested_after_challenge}")
        print(f"  Fix quality: {challenged.fix_quality_score}/5")
        print(f"  Reasoning: {challenged.fix_quality_reasoning}")
        print(f"  Duration: {challenged.duration_ms:.0f}ms")

        # Delta
        delta = challenged.fix_quality_score - baseline.fix_quality_score
        symbol = "+" if delta > 0 else ("=" if delta == 0 else "")
        print(f"\n  DELTA: {symbol}{delta} points")

    # Summary
    print(f"\n{'='*70}")
    print("EXPERIMENT SUMMARY")
    print(f"{'='*70}")

    for scenario in scenarios:
        baseline = next(r for r in all_results if r.scenario == scenario.name and r.condition == "baseline")
        challenged = next(r for r in all_results if r.scenario == scenario.name and r.condition == "challenge")
        delta = challenged.fix_quality_score - baseline.fix_quality_score

        print(f"\n  {scenario.name}:")
        print(f"    Baseline:  {baseline.fix_quality_score}/5 — {baseline.fix_quality_reasoning}")
        print(f"    Challenge: {challenged.fix_quality_score}/5 — {challenged.fix_quality_reasoning}")
        print(f"    Files discovered: {challenged.files_requested_after_challenge}")
        print(f"    Delta: {'+' if delta > 0 else ''}{delta}")
        print(f"    Latency: baseline {baseline.duration_ms:.0f}ms, challenge {challenged.duration_ms:.0f}ms")

    # Hypothesis verdict
    baseline_avg = sum(r.fix_quality_score for r in all_results if r.condition == "baseline") / len(scenarios)
    challenge_avg = sum(r.fix_quality_score for r in all_results if r.condition == "challenge") / len(scenarios)
    exploration_improved = any(
        len(r.files_requested_after_challenge) > 0
        for r in all_results if r.condition == "challenge"
    )

    print(f"\n{'='*70}")
    print("HYPOTHESIS EVALUATION")
    print(f"{'='*70}")
    print(f"\n  H1 (Challenge improves exploration): {'SUPPORTED' if exploration_improved else 'NOT SUPPORTED'}")
    print(f"  H2 (Challenge improves fix quality): avg baseline={baseline_avg:.1f}, challenge={challenge_avg:.1f}")
    if challenge_avg > baseline_avg:
        print(f"      SUPPORTED (+{challenge_avg - baseline_avg:.1f} points avg)")
    elif challenge_avg == baseline_avg:
        print("      NEUTRAL (no improvement)")
    else:
        print(f"      CONTRADICTED ({challenge_avg - baseline_avg:.1f} points avg)")

    # Save raw data
    output_path = _SCENARIOS_DIR / "results.json"
    with open(output_path, "w") as f:
        json.dump(
            [
                {
                    "scenario": r.scenario,
                    "condition": r.condition,
                    "files_shown": r.files_shown,
                    "files_requested": r.files_requested_after_challenge,
                    "fix_quality_score": r.fix_quality_score,
                    "fix_quality_reasoning": r.fix_quality_reasoning,
                    "duration_ms": r.duration_ms,
                    "proposed_fix": r.proposed_fix,
                    "raw_responses": r.raw_responses,
                }
                for r in all_results
            ],
            f,
            indent=2,
        )
    print(f"\n  Raw results saved to: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
