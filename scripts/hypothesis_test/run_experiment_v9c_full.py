"""V9c: full 30-exercise run with the 3 SOUL.md conditions.

Conditions:
  A) none           — no system message
  B) specific       — V9's correctness-focused engineer prompt
  C) generic_agent  — Durin's root SOUL.md (general assistant role)

Differences vs V9:
  - Drops generic_engineer; adds generic_agent (Durin SOUL.md)
  - Captures finish_reason from the API (transparency: "stop" / "length" / etc.)
  - Saves the full generated code per (exercise, condition) for manual review
  - Outputs a divergence summary flagging cases with significant token spread
    so the user can pick which to inspect by hand (no LLM judge — same-model
    judging proved unreliable in the V9b mini-test)
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from run_experiment_v9_soul import (  # type: ignore
    SOUL_SPECIFIC,
    USER_PROMPT_TEMPLATE,
    _DATASET_ROOT,
    _MODEL,
    _OUTPUT_DIR,
    _PYTEST_TIMEOUT_SECONDS,
    _RNG_SEED,
    _SUBSET_SIZE,
    _load_api_key,
    build_user_prompt,
    discover_exercises,
    parse_response,
    run_pytest,
)

_API_BASE = "https://api.z.ai/api/coding/paas/v4"
_DURIN_SOUL_PATH = Path(__file__).parent.parent.parent / "SOUL.md"
_REVIEW_DIR = _OUTPUT_DIR / "v9c_manual_review"


SOUL_NONE = ""
SOUL_GENERIC_AGENT = _DURIN_SOUL_PATH.read_text()

CONDITIONS = [
    ("none", SOUL_NONE),
    ("specific", SOUL_SPECIFIC),
    ("generic_agent", SOUL_GENERIC_AGENT),
]


# --- LLM call with finish_reason capture ---

async def llm_chat_full(
    client: AsyncOpenAI, messages: list[dict], temperature: float = 0.0
) -> dict:
    """Return dict with content, finish_reason, usage; or error info if failed."""
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            resp = await client.chat.completions.create(
                model=_MODEL,
                messages=messages,
                temperature=temperature,
                max_tokens=4096,
            )
            choice = resp.choices[0]
            return {
                "content": choice.message.content or "",
                "finish_reason": getattr(choice, "finish_reason", "unknown"),
                "tokens_input": (resp.usage.prompt_tokens if resp.usage else 0),
                "tokens_output": (resp.usage.completion_tokens if resp.usage else 0),
                "error": None,
            }
        except (InternalServerError, APIConnectionError, RateLimitError) as e:
            last_err = e
            await asyncio.sleep(2 ** attempt * 5)
        except APIStatusError as e:
            if e.status_code in (502, 503, 504):
                last_err = e
                await asyncio.sleep(2 ** attempt * 5)
            else:
                raise
    return {
        "content": "",
        "finish_reason": "api_error",
        "tokens_input": 0,
        "tokens_output": 0,
        "error": f"api retries exhausted: {last_err}",
    }


# --- Trial ---

@dataclass
class Trial:
    exercise: str
    condition: str
    soul_chars: int = 0
    user_prompt_chars: int = 0
    response_raw: str = ""
    parsed_code: dict[str, str] = field(default_factory=dict)
    tokens_input: int = 0
    tokens_output: int = 0
    finish_reason: str = ""
    llm_duration_ms: float = 0.0
    pytest_exit_code: int | None = None
    pytest_output: str = ""
    pytest_duration_ms: float = 0.0
    passed: bool = False
    error: str | None = None
    code_lines: int = 0
    num_functions: int = 0
    num_classes: int = 0


def quick_metrics(code: str) -> dict:
    lines = code.splitlines()
    code_lines = sum(
        1 for ln in lines
        if ln.strip() and not ln.strip().startswith("#")
    )
    num_functions = sum(1 for ln in lines if ln.lstrip().startswith("def "))
    num_classes = sum(1 for ln in lines if ln.lstrip().startswith("class "))
    return {
        "code_lines": code_lines,
        "num_functions": num_functions,
        "num_classes": num_classes,
    }


async def run_one(client: AsyncOpenAI, ex_dir: Path, condition: str, soul: str) -> Trial:
    trial = Trial(exercise=ex_dir.name, condition=condition, soul_chars=len(soul))
    user_prompt, stubs = build_user_prompt(ex_dir)
    trial.user_prompt_chars = len(user_prompt)
    stub_names = [p.name for p in stubs]

    messages: list[dict] = []
    if soul:
        messages.append({"role": "system", "content": soul})
    messages.append({"role": "user", "content": user_prompt})

    t0 = time.time()
    resp = await llm_chat_full(client, messages, temperature=0.0)
    trial.llm_duration_ms = (time.time() - t0) * 1000
    trial.response_raw = resp["content"]
    trial.finish_reason = resp["finish_reason"]
    trial.tokens_input = resp["tokens_input"]
    trial.tokens_output = resp["tokens_output"]
    if resp.get("error"):
        trial.error = resp["error"]
        return trial

    parsed = parse_response(trial.response_raw, stub_names)
    trial.parsed_code = dict(parsed)
    if not parsed:
        trial.error = f"no code blocks parsed (finish_reason={trial.finish_reason})"
        return trial

    primary_code = next(iter(parsed.values()))
    metrics = quick_metrics(primary_code)
    trial.code_lines = metrics["code_lines"]
    trial.num_functions = metrics["num_functions"]
    trial.num_classes = metrics["num_classes"]

    work = Path(tempfile.mkdtemp(prefix=f"v9c_{ex_dir.name}_{condition}_"))
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


# --- Output / divergence analysis ---

def save_review_files(by_exercise: dict[str, list[Trial]]) -> None:
    """Save the generated code per (exercise, condition) for manual review."""
    if _REVIEW_DIR.exists():
        shutil.rmtree(_REVIEW_DIR)
    _REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    for ex, trials in by_exercise.items():
        ex_dir = _REVIEW_DIR / ex
        ex_dir.mkdir(parents=True, exist_ok=True)
        for t in trials:
            if t.parsed_code:
                for fname, content in t.parsed_code.items():
                    base = fname.rsplit(".", 1)
                    suffix = f".{base[1]}" if len(base) == 2 else ""
                    out_file = ex_dir / f"{t.condition}__{base[0]}{suffix}"
                    out_file.write_text(content)
            # summary file per condition
            (ex_dir / f"{t.condition}__summary.txt").write_text(
                f"exercise: {t.exercise}\n"
                f"condition: {t.condition}\n"
                f"passed: {t.passed}\n"
                f"pytest_exit_code: {t.pytest_exit_code}\n"
                f"finish_reason: {t.finish_reason}\n"
                f"tokens_input: {t.tokens_input}\n"
                f"tokens_output: {t.tokens_output}\n"
                f"code_lines: {t.code_lines}\n"
                f"llm_duration_ms: {t.llm_duration_ms:.0f}\n"
                f"error: {t.error or '(none)'}\n"
                f"\n--- PYTEST OUTPUT (tail) ---\n{t.pytest_output[-800:]}\n"
            )


def divergence_summary(by_exercise: dict[str, list[Trial]]) -> list[dict]:
    """Sort exercises by output-token spread between conditions."""
    rows = []
    for ex, trials in by_exercise.items():
        outs = {t.condition: t.tokens_output for t in trials}
        passes = {t.condition: t.passed for t in trials}
        finishes = {t.condition: t.finish_reason for t in trials}
        valid_outs = [v for v in outs.values() if v > 0]
        if len(valid_outs) < 2:
            ratio = 0.0
        else:
            mn = min(valid_outs)
            mx = max(valid_outs)
            ratio = mx / mn if mn > 0 else 0.0
        rows.append({
            "exercise": ex,
            "tokens_out": outs,
            "passed": passes,
            "finish_reason": finishes,
            "ratio_max_min": round(ratio, 2),
            "divergent_pass": len(set(passes.values())) > 1,
        })
    rows.sort(key=lambda r: (not r["divergent_pass"], -r["ratio_max_min"]))
    return rows


# --- Main ---

async def main() -> None:
    api_key = _load_api_key()
    client = AsyncOpenAI(api_key=api_key, base_url=_API_BASE)

    subset = discover_exercises(_SUBSET_SIZE, _RNG_SEED)
    print(f"V9c — 30 exercises × 3 conditions ({len(subset) * len(CONDITIONS)} trials)")
    print(f"Model: {_MODEL} | temp=0 | seed={_RNG_SEED}")
    print(f"Conditions: {[c for c, _ in CONDITIONS]}\n")

    by_exercise: dict[str, list[Trial]] = {}
    jsonl_path = _OUTPUT_DIR / f"results_v9c_seed{_RNG_SEED}.jsonl"
    jsonl_path.write_text("")

    for ex_dir in subset:
        print(f"--- {ex_dir.name} ---")
        trials: list[Trial] = []
        for cond_name, soul in CONDITIONS:
            t = await run_one(client, ex_dir, cond_name, soul)
            trials.append(t)
            status = (
                "PASS" if t.passed
                else (f"err({t.error[:25]})" if t.error else f"FAIL({t.pytest_exit_code})")
            )
            print(f"  {cond_name:<14} {status:<22} fin={t.finish_reason:<10} "
                  f"tok={t.tokens_input}/{t.tokens_output:<5} lines={t.code_lines:<4} "
                  f"{t.llm_duration_ms:.0f}ms")
            with jsonl_path.open("a") as f:
                f.write(json.dumps(asdict(t)) + "\n")
        by_exercise[ex_dir.name] = trials

    # Aggregate
    print(f"\n{'='*70}\nAGGREGATE\n{'='*70}")
    print(f"\n{'Cond':<14} {'Pass':<10} {'Errors':<8} {'AvgTokOut':<10} "
          f"{'finish=length':<14} {'finish=stop':<12}")
    print("-" * 70)
    for cond, _ in CONDITIONS:
        trials = [t for trs in by_exercise.values() for t in trs if t.condition == cond]
        passed = sum(1 for t in trials if t.passed)
        errors = sum(1 for t in trials if t.error)
        avg_tok = sum(t.tokens_output for t in trials) / max(1, len(trials))
        length = sum(1 for t in trials if t.finish_reason == "length")
        stop = sum(1 for t in trials if t.finish_reason == "stop")
        print(f"{cond:<14} {passed}/{len(trials):<8} {errors:<8} "
              f"{avg_tok:<10.0f} {length:<14} {stop:<12}")

    # Divergence
    rows = divergence_summary(by_exercise)
    print(f"\n{'='*70}\nDIVERGENCE — sorted by pass-diff first, then token ratio\n{'='*70}\n")
    print(f"{'exercise':<24} {'pass_n/s/g':<13} {'tok_n/s/g':<22} {'ratio':<6}")
    print("-" * 70)
    for r in rows:
        passes = "{}/{}/{}".format(*(
            "P" if r["passed"][c] else "F" for c in ("none", "specific", "generic_agent")
        ))
        toks = "{}/{}/{}".format(*(
            r["tokens_out"][c] for c in ("none", "specific", "generic_agent")
        ))
        marker = " ←" if r["divergent_pass"] else ""
        print(f"{r['exercise']:<24} {passes:<13} {toks:<22} {r['ratio_max_min']:<6}{marker}")

    save_review_files(by_exercise)
    print(f"\nFull JSONL: {jsonl_path}")
    print(f"Manual-review code dump: {_REVIEW_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
