"""Hypothesis test V2: Single-call challenge vs multi-turn vs baseline.

V1 showed: multi-turn challenge identifies gaps but degrades output (empty responses).
V2 tests: does integrating the challenge INTO a single prompt fix this?

Three conditions:
  A) BASELINE: Bug + file → fix
  B) INTEGRATED: Bug + file + challenge-in-prompt → fix (single call)
  C) FULL_CONTEXT: Bug + ALL files → fix (upper bound)

Usage:
    .venv/bin/python scripts/hypothesis_test/run_experiment_v2.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI

_ENV_PATH = Path.home() / ".hermes" / ".env"
_API_BASE = "https://api.z.ai/api/coding/paas/v4"
_MODEL = "glm-5.1"
_SCENARIOS_DIR = Path(__file__).parent


def _load_api_key() -> str:
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            if line.startswith("ZAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("ZAI_API_KEY", "")


@dataclass
class Result:
    scenario: str
    condition: str
    fix_quality_score: int = 0
    fix_quality_reasoning: str = ""
    mentions_discount: bool = False  # scenario 2 specific
    mentions_cache_invalidation: bool = False  # scenario 1 specific
    files_mentioned: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    proposed_fix: str = ""


def load_file(scenario_dir: Path, name: str) -> str:
    return (scenario_dir / name).read_text()


def format_file(name: str, content: str) -> str:
    return f"### {name}\n```python\n{content}\n```"


SYSTEM = "You are a senior software engineer. Analyze the bug and propose a specific fix."

CHALLENGE_INTEGRATED = """\
IMPORTANT: Before proposing a fix, you MUST first complete this analysis:

## Pre-Fix Analysis (required)
1. FILES NOT YET SEEN: List every file mentioned in the bug report or imported \
in the code that you haven't read. For each, state what relevant information \
it might contain.
2. ASSUMPTIONS: List what you're assuming about code you haven't verified.
3. RISKS: What's the most likely way your fix could be WRONG?

## Proposed Fix
Only AFTER completing the analysis above, propose your fix. Be specific about \
what code to change and why. Address any risks you identified."""

JUDGE_PROMPT = """\
Rate this bug fix from 1-5 against the ground truth:
5 = Correct root cause + complete fix
4 = Correct root cause, minor gap
3 = Partially correct, misses key aspect
2 = Wrong approach
1 = No fix provided or completely wrong

Ground truth: {ground_truth}

Proposed fix: {fix}

Respond as JSON: {{"score": N, "reasoning": "one sentence"}}"""


# --- Scenarios ---

SCENARIOS = [
    {
        "name": "notification_cache_bug",
        "dir": "scenario_1",
        "bug": (
            "Bug: Users don't receive email notifications after updating their "
            "email address. Notifications resume after ~1 hour.\n"
            "Files: sender.py, preferences.py, templates.py, user_service.py"
        ),
        "initial_files": ["sender.py"],
        "all_files": ["sender.py", "preferences.py", "templates.py", "user_service.py"],
        "ground_truth": (
            "Root cause: user_service.update_user_profile() changes email in DB "
            "but never calls invalidate_cache() from preferences.py. Cache (1hr TTL) "
            "serves stale email. Fix: call invalidate_cache(user_id) after DB update."
        ),
        "check_keyword": "invalidate",
    },
    {
        "name": "invoice_tax_integration",
        "dir": "scenario_2",
        "bug": (
            "Task: generate_invoice() uses hardcoded 10% tax. Need regional rates "
            "from order['region'].\n"
            "Files: invoice.py, tax_rules.py, discounts.py, test_invoice.py"
        ),
        "initial_files": ["invoice.py"],
        "all_files": ["invoice.py", "tax_rules.py", "discounts.py", "test_invoice.py"],
        "ground_truth": (
            "Complete fix: (1) Use get_tax_rate() from tax_rules.py. "
            "(2) Handle tax-exempt items via is_tax_exempt(). "
            "(3) Apply discounts BEFORE tax using apply_discount() from discounts.py. "
            "(4) Process discount_code from order dict. "
            "A fix that only swaps tax rate but ignores discounts is incomplete."
        ),
        "check_keyword": "discount",
    },
]


async def llm(client: AsyncOpenAI, messages: list[dict], temp: float = 0.3) -> str:
    resp = await client.chat.completions.create(
        model=_MODEL, messages=messages, temperature=temp, max_tokens=2048,
    )
    return resp.choices[0].message.content or ""


async def run_condition(
    client: AsyncOpenAI, scenario: dict, condition: str
) -> Result:
    t0 = time.time()
    sdir = _SCENARIOS_DIR / scenario["dir"]

    if condition == "baseline":
        files = scenario["initial_files"]
        user_suffix = "Propose a fix. Be specific about what code to change and why."
    elif condition == "integrated":
        files = scenario["initial_files"]
        user_suffix = CHALLENGE_INTEGRATED
    elif condition == "full_context":
        files = scenario["all_files"]
        user_suffix = "Propose a fix. Be specific about what code to change and why."
    else:
        raise ValueError(f"Unknown condition: {condition}")

    file_contents = "\n\n".join(
        format_file(f, load_file(sdir, f)) for f in files
    )

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"## Bug Report\n{scenario['bug']}\n\n"
            f"## Source Code\n{file_contents}\n\n"
            f"{user_suffix}"
        )},
    ]

    fix = await llm(client, messages)

    # Check for key insights
    fix_lower = fix.lower()
    hidden_files = [f for f in scenario["all_files"] if f not in scenario["initial_files"]]
    files_mentioned = [f for f in hidden_files if f.replace(".py", "") in fix_lower]

    result = Result(
        scenario=scenario["name"],
        condition=condition,
        files_mentioned=files_mentioned,
        duration_ms=(time.time() - t0) * 1000,
        proposed_fix=fix,
    )

    # Scenario-specific checks
    result.mentions_discount = "discount" in fix_lower
    result.mentions_cache_invalidation = "invalidat" in fix_lower

    return result


async def judge(client: AsyncOpenAI, scenario: dict, result: Result) -> None:
    prompt = JUDGE_PROMPT.format(
        ground_truth=scenario["ground_truth"],
        fix=result.proposed_fix[:3000],
    )
    response = await llm(client, [
        {"role": "system", "content": "Rate the fix quality. Respond only with JSON."},
        {"role": "user", "content": prompt},
    ], temp=0.1)

    try:
        text = response
        if "```" in text:
            text = text.split("```")[1].strip()
            if text.startswith("json"):
                text = text[4:].strip()
        data = json.loads(text)
        result.fix_quality_score = int(data.get("score", 0))
        result.fix_quality_reasoning = data.get("reasoning", response[:200])
    except (json.JSONDecodeError, ValueError):
        result.fix_quality_reasoning = f"(parse error) {response[:200]}"


async def main():
    api_key = _load_api_key()
    if not api_key:
        print("ERROR: No API key found")
        return
    client = AsyncOpenAI(api_key=api_key, base_url=_API_BASE)

    conditions = ["baseline", "integrated", "full_context"]
    all_results: list[Result] = []

    for scenario in SCENARIOS:
        print(f"\n{'='*70}")
        print(f"SCENARIO: {scenario['name']}")
        print(f"{'='*70}")

        for cond in conditions:
            print(f"\n--- {cond.upper()} ---")
            result = await run_condition(client, scenario, cond)
            await judge(client, scenario, result)
            all_results.append(result)

            keyword_check = (
                result.mentions_cache_invalidation
                if scenario["name"] == "notification_cache_bug"
                else result.mentions_discount
            )

            print(f"  Score: {result.fix_quality_score}/5")
            print(f"  Reasoning: {result.fix_quality_reasoning}")
            print(f"  Hidden files mentioned: {result.files_mentioned}")
            print(f"  Key insight ({scenario['check_keyword']}): {'YES' if keyword_check else 'NO'}")
            print(f"  Duration: {result.duration_ms:.0f}ms")

    # Summary table
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"\n{'Scenario':<30} {'Condition':<15} {'Score':<8} {'Key Insight':<12} {'Hidden Files'}")
    print("-" * 90)

    for r in all_results:
        scenario = next(s for s in SCENARIOS if s["name"] == r.scenario)
        keyword_hit = (
            r.mentions_cache_invalidation
            if r.scenario == "notification_cache_bug"
            else r.mentions_discount
        )
        print(
            f"{r.scenario:<30} {r.condition:<15} {r.fix_quality_score}/5"
            f"{'':>4} {'YES' if keyword_hit else 'NO':<12} {r.files_mentioned}"
        )

    # Aggregate
    print(f"\n{'='*70}")
    print("HYPOTHESIS EVALUATION")
    print(f"{'='*70}")

    for cond in conditions:
        cond_results = [r for r in all_results if r.condition == cond]
        avg = sum(r.fix_quality_score for r in cond_results) / len(cond_results)
        insights = sum(
            1 for r in cond_results
            if (r.mentions_cache_invalidation if r.scenario == "notification_cache_bug"
                else r.mentions_discount)
        )
        print(f"\n  {cond.upper():<15} avg_score={avg:.1f}/5  key_insights={insights}/{len(cond_results)}")

    baseline_avg = sum(r.fix_quality_score for r in all_results if r.condition == "baseline") / len(SCENARIOS)
    integrated_avg = sum(r.fix_quality_score for r in all_results if r.condition == "integrated") / len(SCENARIOS)
    full_avg = sum(r.fix_quality_score for r in all_results if r.condition == "full_context") / len(SCENARIOS)

    print(f"\n  H1 (Integrated challenge > baseline): ", end="")
    if integrated_avg > baseline_avg:
        print(f"SUPPORTED (+{integrated_avg - baseline_avg:.1f})")
    elif integrated_avg == baseline_avg:
        print("NEUTRAL")
    else:
        print(f"NOT SUPPORTED ({integrated_avg - baseline_avg:.1f})")

    print(f"  H2 (Full context is upper bound): full={full_avg:.1f} vs integrated={integrated_avg:.1f}")
    print(f"  H3 (Challenge closes gap to full context): ", end="")
    gap_before = full_avg - baseline_avg
    gap_after = full_avg - integrated_avg
    if gap_before > 0:
        pct = (1 - gap_after / gap_before) * 100
        print(f"Gap closed: {pct:.0f}%")
    else:
        print("No gap (baseline = full context)")

    # Save
    output = _SCENARIOS_DIR / "results_v2.json"
    with open(output, "w") as f:
        json.dump([{
            "scenario": r.scenario, "condition": r.condition,
            "score": r.fix_quality_score, "reasoning": r.fix_quality_reasoning,
            "files_mentioned": r.files_mentioned,
            "mentions_discount": r.mentions_discount,
            "mentions_invalidation": r.mentions_cache_invalidation,
            "duration_ms": r.duration_ms,
            "fix_excerpt": r.proposed_fix[:1000],
        } for r in all_results], f, indent=2)
    print(f"\n  Results saved to {output}")


if __name__ == "__main__":
    asyncio.run(main())
