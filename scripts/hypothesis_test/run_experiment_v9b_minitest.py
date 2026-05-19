"""V9b mini-test: ONE exercise, 3 conditions, full telemetry.

Goal: validate the V9b setup (full code capture + LLM-judge for quality)
on a single representative exercise before scaling to the full subset.

Conditions:
  A) none           — no system message
  B) specific       — V9's correctness-focused engineer prompt
  C) generic_agent  — Durin's root SOUL.md (general assistant role)

Captures everything: full prompts, full response, parsed code, pytest output,
LLM-judge scores per dimension, code metrics, latencies.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from openai import AsyncOpenAI

from run_experiment_v9_soul import (  # type: ignore
    SOUL_SPECIFIC,
    USER_PROMPT_TEMPLATE,
    _API_BASE,
    _MODEL,
    _PYTEST_TIMEOUT_SECONDS,
    _load_api_key,
    build_user_prompt,
    llm_chat_with_usage,
    parse_response,
    run_pytest,
)

_DATASET_ROOT = Path(__file__).parent / "exercism_python" / "exercises" / "practice"
_OUTPUT_DIR = Path(__file__).parent / "v9_runs"
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_DURIN_SOUL_PATH = Path(__file__).parent.parent.parent / "SOUL.md"

TARGET_EXERCISES = ["zipper", "knapsack"]


# --- Three conditions ---

SOUL_NONE = ""

SOUL_GENERIC_AGENT = _DURIN_SOUL_PATH.read_text()  # Durin's root SOUL.md

CONDITIONS = [
    ("none", SOUL_NONE),
    ("specific", SOUL_SPECIFIC),
    ("generic_agent", SOUL_GENERIC_AGENT),
]


# --- LLM judge ---

JUDGE_SYSTEM = """\
You are a senior code reviewer evaluating a Python solution to a programming exercise.
Rate the solution on FIVE dimensions, INDEPENDENTLY of whether tests pass or fail.

Output STRICT JSON:
{
  "correctness_reasoning": {"score": 1-5, "comment": "one sentence"},
  "conciseness":           {"score": 1-5, "comment": "one sentence"},
  "edge_case_handling":    {"score": 1-5, "comment": "one sentence"},
  "idiomaticity":          {"score": 1-5, "comment": "one sentence"},
  "readability":           {"score": 1-5, "comment": "one sentence"},
  "overall":               {"score": 1-5, "comment": "one sentence summary"}
}

Score guide (apply per dimension):
  5 = exemplary; nothing meaningful to improve
  4 = good; minor issues
  3 = acceptable; clear room to improve
  2 = weak; multiple problems
  1 = bad; would not pass a review

Definitions:
- correctness_reasoning: did the author appear to reason about correctness (handle edge cases, follow spec literally)?
- conciseness: is it as short as it can be without losing clarity? Penalize bloat.
- edge_case_handling: are explicit checks for empty/None/boundary inputs present where the problem implies them?
- idiomaticity: Pythonic style (list comprehensions, generators, idioms vs. translated-from-Java).
- readability: variable names, structure, comments where useful.

Judge the CODE only. Tests may or may not pass — that's measured separately."""


@dataclass
class CodeMetrics:
    total_lines: int = 0
    code_lines: int = 0
    blank_lines: int = 0
    comment_lines: int = 0
    num_functions: int = 0
    num_classes: int = 0
    has_docstrings: bool = False
    has_type_hints: bool = False
    has_try_except: bool = False


def compute_metrics(code: str) -> CodeMetrics:
    m = CodeMetrics()
    in_docstring = False
    for raw in code.splitlines():
        line = raw.rstrip()
        m.total_lines += 1
        stripped = line.strip()
        if not stripped:
            m.blank_lines += 1
            continue
        if stripped.startswith("#"):
            m.comment_lines += 1
            continue
        if '"""' in stripped or "'''" in stripped:
            m.has_docstrings = True
        m.code_lines += 1
        if stripped.startswith("def "):
            m.num_functions += 1
            if "->" in stripped or ":" in stripped.split("(", 1)[-1]:
                # rough: presence of colon-after-arg suggests annotation
                if re.search(r":\s*\w", stripped.split("(", 1)[-1]):
                    m.has_type_hints = True
        if stripped.startswith("class "):
            m.num_classes += 1
        if "try:" in stripped or stripped.startswith("except"):
            m.has_try_except = True
    return m


@dataclass
class JudgeScores:
    correctness_reasoning: int = 0
    conciseness: int = 0
    edge_case_handling: int = 0
    idiomaticity: int = 0
    readability: int = 0
    overall: int = 0
    comments: dict = field(default_factory=dict)
    raw_response: str = ""


async def run_judge(client: AsyncOpenAI, problem: str, code: str) -> JudgeScores:
    user_msg = (
        f"## Problem\n{problem}\n\n"
        f"## Submitted Code\n```python\n{code}\n```\n\n"
        "Output STRICT JSON only."
    )
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    js = JudgeScores()
    try:
        msg, _ = await llm_chat_with_usage(client, messages, tools=[], temperature=0.0)
        js.raw_response = msg.content or ""
    except Exception as e:
        js.raw_response = f"(error: {e})"
        return js
    text = js.raw_response
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
    except json.JSONDecodeError:
        return js
    for key in ("correctness_reasoning", "conciseness", "edge_case_handling",
                "idiomaticity", "readability", "overall"):
        entry = data.get(key, {})
        if isinstance(entry, dict):
            setattr(js, key, int(entry.get("score", 0)))
            js.comments[key] = str(entry.get("comment", ""))[:300]
    return js


# --- Per-condition run ---

@dataclass
class FullTrial:
    exercise: str
    condition: str
    system_prompt: str = ""
    user_prompt: str = ""
    response_raw: str = ""
    parsed_code: dict = field(default_factory=dict)
    tokens_input: int = 0
    tokens_output: int = 0
    llm_duration_ms: float = 0.0
    pytest_exit_code: int | None = None
    pytest_output: str = ""
    pytest_duration_ms: float = 0.0
    passed: bool = False
    error: str | None = None
    metrics: dict = field(default_factory=dict)
    judge_scores: dict = field(default_factory=dict)


async def run_one(
    client: AsyncOpenAI, ex_dir: Path, condition: str, soul: str,
) -> FullTrial:
    trial = FullTrial(exercise=ex_dir.name, condition=condition, system_prompt=soul)
    user_prompt, stubs = build_user_prompt(ex_dir)
    trial.user_prompt = user_prompt
    stub_names = [p.name for p in stubs]

    messages: list[dict] = []
    if soul:
        messages.append({"role": "system", "content": soul})
    messages.append({"role": "user", "content": user_prompt})

    t0 = time.time()
    try:
        msg, usage = await llm_chat_with_usage(client, messages, tools=[], temperature=0.0)
    except Exception as e:
        trial.error = f"llm error: {e}"
        trial.llm_duration_ms = (time.time() - t0) * 1000
        return trial
    trial.llm_duration_ms = (time.time() - t0) * 1000
    trial.tokens_input = usage.get("prompt_tokens", 0)
    trial.tokens_output = usage.get("completion_tokens", 0)
    trial.response_raw = msg.content or ""

    parsed = parse_response(trial.response_raw, stub_names)
    trial.parsed_code = dict(parsed)
    if not parsed:
        trial.error = "no code blocks parsed"
        return trial

    # Metrics on first file
    primary_code = next(iter(parsed.values()))
    trial.metrics = asdict(compute_metrics(primary_code))

    # Run pytest in temp dir
    work = Path(tempfile.mkdtemp(prefix=f"v9b_{ex_dir.name}_{condition}_"))
    try:
        shutil.copytree(ex_dir, work, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".meta"))
        for name, content in parsed.items():
            (work / name).write_text(content)
        tp0 = time.time()
        exit_code, output = run_pytest(work)
        trial.pytest_duration_ms = (time.time() - tp0) * 1000
        trial.pytest_exit_code = exit_code
        trial.pytest_output = output
        trial.passed = (exit_code == 0)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return trial


# --- Main ---

def print_side_by_side(target: str, trials: list[FullTrial]) -> None:
    print(f"\n{'='*82}\nSIDE-BY-SIDE — {target}\n{'='*82}")
    print(f"{'metric':<32} {'none':<16} {'specific':<16} {'generic_agent':<16}")
    print("-" * 82)
    def val_for(t: FullTrial, key: str):
        if hasattr(t, key):
            return getattr(t, key)
        if t.metrics and key in t.metrics:
            return t.metrics[key]
        if t.judge_scores and key in t.judge_scores:
            return t.judge_scores[key]
        return None
    def row(label: str, key: str) -> None:
        vals = [val_for(t, key) for t in trials]
        cells = [(str(v) if v is not None else "-") for v in vals]
        print(f"  {label:<30} {cells[0]:<16} {cells[1]:<16} {cells[2]:<16}")
    row("passed", "passed")
    row("pytest_exit_code", "pytest_exit_code")
    row("tokens_input", "tokens_input")
    row("tokens_output", "tokens_output")
    row("code_lines", "code_lines")
    row("num_functions", "num_functions")
    row("num_classes", "num_classes")
    row("has_docstrings", "has_docstrings")
    row("has_type_hints", "has_type_hints")
    row("has_try_except", "has_try_except")
    row("judge.correctness_reasoning", "correctness_reasoning")
    row("judge.conciseness", "conciseness")
    row("judge.edge_case_handling", "edge_case_handling")
    row("judge.idiomaticity", "idiomaticity")
    row("judge.readability", "readability")
    row("judge.overall", "overall")


async def main() -> None:
    api_key = _load_api_key()
    client = AsyncOpenAI(api_key=api_key, base_url=_API_BASE)

    print(f"V9b MINI-TEST — exercises: {TARGET_EXERCISES}")
    print(f"Model: {_MODEL} | Temp: 0\n")

    all_by_exercise: dict[str, list[FullTrial]] = {}

    for target in TARGET_EXERCISES:
        ex_dir = _DATASET_ROOT / target
        if not ex_dir.exists():
            print(f"  SKIP: exercise not found: {ex_dir}")
            continue

        problem = ""
        for f in ["introduction.md", "instructions.md", "instructions.append.md"]:
            p = ex_dir / ".docs" / f
            if p.exists():
                problem += p.read_text() + "\n\n"
        problem = problem.strip()

        print(f"\n{'#'*70}\n# Exercise: {target}\n{'#'*70}")
        trials: list[FullTrial] = []
        for cond_name, soul in CONDITIONS:
            print(f"--- {cond_name} ---")
            trial = await run_one(client, ex_dir, cond_name, soul)
            if trial.parsed_code and not trial.error:
                primary = next(iter(trial.parsed_code.values()))
                js = await run_judge(client, problem, primary)
                trial.judge_scores = {
                    "correctness_reasoning": js.correctness_reasoning,
                    "conciseness": js.conciseness,
                    "edge_case_handling": js.edge_case_handling,
                    "idiomaticity": js.idiomaticity,
                    "readability": js.readability,
                    "overall": js.overall,
                    "comments": js.comments,
                    "raw": js.raw_response[:1500],
                }
            trials.append(trial)
            status = "PASS" if trial.passed else ("err" if trial.error else f"FAIL({trial.pytest_exit_code})")
            overall = trial.judge_scores.get("overall", "-") if trial.judge_scores else "-"
            print(f"  status: {status}  judge_overall: {overall}/5  "
                  f"tokens: {trial.tokens_input}/{trial.tokens_output}  "
                  f"code_lines: {trial.metrics.get('code_lines', '-')}  "
                  f"llm: {trial.llm_duration_ms:.0f}ms  pytest: {trial.pytest_duration_ms:.0f}ms")

        all_by_exercise[target] = trials
        print_side_by_side(target, trials)

    output_path = _OUTPUT_DIR / "results_v9b_minitest.json"
    output_path.write_text(json.dumps(
        {ex: [asdict(t) for t in trs] for ex, trs in all_by_exercise.items()},
        indent=2,
    ))
    print(f"\nFull trace: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
