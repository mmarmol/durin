"""V9: Replicate Aider's Exercism benchmark methodology with SOUL.md as the variable.

Tests whether system-prompt specificity affects model performance on a
standard coding benchmark (Aider published +33-41 points from prompt
wording alone with GPT-4). We use the same dataset (133 Exercism Python
exercises) and the same evaluation (pytest exit code) but vary the
SYSTEM PROMPT (SOUL.md) across three conditions:

  A) NONE     — empty system prompt
  B) GENERIC  — short generic engineering posture
  C) SPECIFIC — detailed correctness-focused rules

Edit format: whole-file (model returns complete modified file in a code block).
This is simpler than diff-based formats and isolates the SOUL effect from
edit-format complexity.

Single LLM call per (exercise, condition). One trial per cell.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from openai import AsyncOpenAI

_API_BASE = "https://api.z.ai/api/coding/paas/v4"
_MODEL = "glm-5.1"
_ENV_PATH = Path.home() / ".hermes" / ".env"


def _load_api_key() -> str:
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            if line.startswith("ZAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("ZAI_API_KEY", "")


async def llm_chat_with_usage(client, messages, tools, temperature):
    """Single chat call with retries on transient 5xx errors."""
    from openai import APIConnectionError, APIStatusError, InternalServerError, RateLimitError

    last_err: Exception | None = None
    for attempt in range(4):
        try:
            kwargs = dict(model=_MODEL, messages=messages, temperature=temperature, max_tokens=4096)
            if tools:
                kwargs["tools"] = tools
            resp = await client.chat.completions.create(**kwargs)
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
_DATASET_ROOT = Path(__file__).parent / "exercism_python" / "exercises" / "practice"
_OUTPUT_DIR = Path(__file__).parent / "v9_runs"
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

_PYTEST_TIMEOUT_SECONDS = 60
_RNG_SEED = 42
_SUBSET_SIZE = 30  # Use a subset for initial validation; scale up after signal


# --- SOUL.md content for each condition ---

SOUL_NONE = ""

SOUL_GENERIC = (
    "You are a careful Python engineer. "
    "Write clean, idiomatic Python that follows PEP 8."
)

SOUL_SPECIFIC = """\
You are a Python engineer solving programming exercises. Maximize correctness \
by following these rules:

1. READ THE PROBLEM CAREFULLY, twice if needed. Identify explicit requirements \
AND implicit ones:
   - Required exception types (ValueError, TypeError, etc.)
   - Edge cases mentioned in examples (empty input, zero, negatives, very large)
   - Whether to RETURN a value vs. PRINT it
   - Required function/class names — preserve them EXACTLY from the stub

2. HANDLE EDGE CASES PROACTIVELY:
   - Empty inputs (lists, strings, dicts)
   - Boundary values: 0, 1, -1, very large numbers
   - None / null values where applicable
   - Single-element collections
   - Inputs that should raise an exception per the spec

3. FOLLOW SPECIFICATIONS LITERALLY:
   - If the problem says "raise ValueError", raise exactly ValueError
   - If it says "return X", return X — don't print, don't yield
   - Match the exact function signature from the stub (don't add or remove parameters)

4. USE STANDARD LIBRARY ONLY — no external packages.

5. VERIFY YOUR SOLUTION MENTALLY against the problem's examples before finalizing.\
"""


# --- Prompt construction ---

USER_PROMPT_TEMPLATE = """\
# Problem instructions

{instructions}

# Files to modify

{files_block}

# Output format

Modify the supplied file(s) to solve the problem. Don't change function or class names \
(tests reference them). Only use the Python standard library.

For each file you modify, respond with the FULL contents in this exact format \
(filename on its own line, then a Python fenced code block):

filename.py
```python
<complete file contents>
```

Multiple files: repeat the pattern. Do not include any other commentary outside the code blocks."""


# --- Data structures ---

@dataclass
class ExerciseRun:
    exercise: str
    condition: str
    stub_files: list[str]
    instructions_chars: int = 0
    prompt_chars: int = 0
    response_chars: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    duration_ms: float = 0.0
    parsed_files: list[str] = field(default_factory=list)
    pytest_exit_code: int | None = None
    pytest_output: str = ""
    passed: bool = False
    error: str | None = None


# --- Helpers ---

def discover_exercises(subset_size: int | None, seed: int) -> list[Path]:
    """Return sorted list of practice exercise directories, optionally sampled."""
    if not _DATASET_ROOT.exists():
        raise SystemExit(
            f"Dataset not found at {_DATASET_ROOT}. "
            "Run: git clone --depth=1 https://github.com/exercism/python "
            "scripts/hypothesis_test/exercism_python"
        )
    all_dirs = sorted(d for d in _DATASET_ROOT.iterdir() if d.is_dir())
    if subset_size is None or subset_size >= len(all_dirs):
        return all_dirs
    rng = random.Random(seed)
    return sorted(rng.sample(all_dirs, subset_size))


def read_instructions(ex_dir: Path) -> str:
    parts: list[str] = []
    intro = ex_dir / ".docs" / "introduction.md"
    if intro.exists():
        parts.append(intro.read_text())
    instructions = ex_dir / ".docs" / "instructions.md"
    if instructions.exists():
        parts.append(instructions.read_text())
    appendix = ex_dir / ".docs" / "instructions.append.md"
    if appendix.exists():
        parts.append(appendix.read_text())
    return "\n\n".join(parts).strip()


def stub_files(ex_dir: Path) -> list[Path]:
    """Return *.py files in the exercise that are NOT test files."""
    return sorted(
        p for p in ex_dir.glob("*.py")
        if not p.name.endswith("_test.py")
    )


def build_user_prompt(ex_dir: Path) -> tuple[str, list[Path]]:
    instructions = read_instructions(ex_dir)
    stubs = stub_files(ex_dir)
    blocks = []
    for p in stubs:
        blocks.append(f"{p.name}\n```python\n{p.read_text()}\n```")
    files_block = "\n\n".join(blocks)
    return USER_PROMPT_TEMPLATE.format(
        instructions=instructions, files_block=files_block,
    ), stubs


# --- Response parsing ---

_FILE_BLOCK_RE = re.compile(
    r"^([\w.\-]+\.py)\s*\n+```(?:python|py)?\s*\n([\s\S]*?)\n```",
    re.MULTILINE,
)


def parse_response(response: str, expected_files: list[str]) -> dict[str, str]:
    """Extract {filename: content} pairs from the model response.

    Looks for `filename.py` on its own line followed by a fenced Python code block.
    Filters to only files we expected (so the model can't sneak in test files).
    """
    parsed: dict[str, str] = {}
    expected_set = set(expected_files)
    for match in _FILE_BLOCK_RE.finditer(response):
        name = match.group(1).strip()
        if name in expected_set:
            parsed[name] = match.group(2)
    # Fallback: if the response is just one code block and we expected one file,
    # accept it. Handles the case where the model omits the filename header.
    if not parsed and len(expected_files) == 1:
        loose = re.search(r"```(?:python|py)?\s*\n([\s\S]*?)\n```", response)
        if loose:
            parsed[expected_files[0]] = loose.group(1)
    return parsed


# --- Test execution ---

def run_pytest(work_dir: Path) -> tuple[int, str]:
    """Run pytest in *work_dir*. Returns (exit_code, combined_output).

    Uses Popen + new process group so that on timeout we can SIGKILL the
    entire group. subprocess.run(timeout=...) only signals the immediate
    child, which is not enough when the model emits code with an infinite
    loop (pytest spawns its own subprocesses and the runaway worker keeps
    running, blocking the harness forever — observed as a 6.5h hang in V9e).
    """
    proc = subprocess.Popen(
        ["python", "-m", "pytest", "-x", "--tb=short"],
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        start_new_session=True,  # new process group; needed for killpg below
    )
    try:
        stdout, _ = proc.communicate(timeout=_PYTEST_TIMEOUT_SECONDS)
        return proc.returncode, (stdout or "")[-3000:]
    except subprocess.TimeoutExpired:
        # Hard-kill the whole process group. SIGTERM first for graceful shutdown,
        # then SIGKILL after a brief grace period if anything is still alive.
        pgid = None
        with contextlib.suppress(Exception):
            pgid = os.getpgid(proc.pid)
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGTERM)
            try:
                stdout, _ = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    proc.communicate(timeout=2)
                stdout = ""
        else:
            with contextlib.suppress(Exception):
                proc.kill()
            stdout = ""
        return -1, f"pytest timed out after {_PYTEST_TIMEOUT_SECONDS}s (force-killed)"
    except Exception as e:  # pragma: no cover
        with contextlib.suppress(Exception):
            proc.kill()
        return -2, f"pytest exec error: {e}"


# --- Single trial ---

async def run_one(
    client: AsyncOpenAI,
    ex_dir: Path,
    condition: str,
    soul_content: str,
) -> ExerciseRun:
    """Run one (exercise, condition) trial. Returns a populated ExerciseRun."""
    user_prompt, stubs = build_user_prompt(ex_dir)
    stub_names = [p.name for p in stubs]
    run = ExerciseRun(
        exercise=ex_dir.name,
        condition=condition,
        stub_files=stub_names,
        instructions_chars=sum(p.stat().st_size for p in stubs),
        prompt_chars=len(user_prompt),
    )

    messages: list[dict] = []
    if soul_content:
        messages.append({"role": "system", "content": soul_content})
    messages.append({"role": "user", "content": user_prompt})

    t0 = time.time()
    try:
        msg, usage = await llm_chat_with_usage(client, messages, tools=[], temperature=0.0)
    except Exception as e:
        run.error = f"llm error: {e}"
        run.duration_ms = (time.time() - t0) * 1000
        return run
    run.duration_ms = (time.time() - t0) * 1000
    run.tokens_input = usage.get("prompt_tokens", 0)
    run.tokens_output = usage.get("completion_tokens", 0)
    response = msg.content or ""
    run.response_chars = len(response)

    parsed = parse_response(response, stub_names)
    run.parsed_files = sorted(parsed.keys())
    if not parsed:
        run.error = "no code blocks extracted from response"
        return run

    # Copy exercise to a temp dir (excluding .meta which has the answer)
    work_dir = Path(tempfile.mkdtemp(prefix=f"v9_{ex_dir.name}_"))
    try:
        shutil.copytree(ex_dir, work_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".meta"))
        for name, content in parsed.items():
            (work_dir / name).write_text(content)
        exit_code, output = run_pytest(work_dir)
        run.pytest_exit_code = exit_code
        run.pytest_output = output
        run.passed = (exit_code == 0)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return run


# --- Main ---

CONDITIONS = [
    ("none", SOUL_NONE),
    ("generic", SOUL_GENERIC),
    ("specific", SOUL_SPECIFIC),
]


async def main() -> None:
    api_key = _load_api_key()
    client = AsyncOpenAI(api_key=api_key, base_url=_API_BASE)

    subset = discover_exercises(_SUBSET_SIZE, _RNG_SEED)
    print(f"V9 — SOUL.md effect on Aider Exercism benchmark")
    print(f"Model: {_MODEL} | Temp: 0 | Subset: {len(subset)} exercises | Seed: {_RNG_SEED}")
    print(f"Conditions: {[c for c, _ in CONDITIONS]}")
    print()

    all_runs: list[ExerciseRun] = []
    results_path = _OUTPUT_DIR / f"results_v9_seed{_RNG_SEED}.jsonl"
    # Truncate output
    results_path.write_text("")

    for ex_dir in subset:
        ex_name = ex_dir.name
        print(f"--- {ex_name} ---")
        per_cond: dict[str, ExerciseRun] = {}
        for cond_name, soul in CONDITIONS:
            run = await run_one(client, ex_dir, cond_name, soul)
            per_cond[cond_name] = run
            status = "PASS" if run.passed else ("err" if run.error else f"FAIL({run.pytest_exit_code})")
            print(f"  {cond_name:<10} {status:<12} iters tokens={run.tokens_input}/{run.tokens_output} {run.duration_ms:.0f}ms")
            all_runs.append(run)
            with results_path.open("a") as f:
                f.write(json.dumps(asdict(run)) + "\n")

    # Aggregate
    print(f"\n{'='*60}\nAGGREGATE — {len(subset)} exercises × {len(CONDITIONS)} conditions\n{'='*60}")
    print(f"\n{'Condition':<12} {'Pass rate':<12} {'Passed':<10} {'Errors':<8} {'Avg tokens':<12}")
    print("-" * 60)
    for cond_name, _ in CONDITIONS:
        cond_runs = [r for r in all_runs if r.condition == cond_name]
        passed = sum(1 for r in cond_runs if r.passed)
        errors = sum(1 for r in cond_runs if r.error)
        avg_tokens = sum(r.tokens_input + r.tokens_output for r in cond_runs) / max(1, len(cond_runs))
        print(f"{cond_name:<12} {passed/len(cond_runs)*100:>6.1f}%      {passed}/{len(cond_runs):<8} {errors:<8} {avg_tokens:.0f}")

    # Pairwise comparison (which exercises differ across conditions)
    print(f"\n{'='*60}\nDelta analysis (exercises where conditions diverged)\n{'='*60}")
    by_ex: dict[str, dict[str, bool]] = {}
    for r in all_runs:
        by_ex.setdefault(r.exercise, {})[r.condition] = r.passed
    diverged = []
    for ex, results in by_ex.items():
        vals = set(results.values())
        if len(vals) > 1:
            diverged.append((ex, results))
    print(f"\n{len(diverged)} of {len(by_ex)} exercises diverged between conditions\n")
    for ex, results in diverged[:20]:
        print(f"  {ex:<32} none={results.get('none', '-')!s:<6} generic={results.get('generic', '-')!s:<6} specific={results.get('specific', '-')!s}")
    if len(diverged) > 20:
        print(f"  ... and {len(diverged) - 20} more")

    print(f"\nFull trace: {results_path}")


if __name__ == "__main__":
    asyncio.run(main())
