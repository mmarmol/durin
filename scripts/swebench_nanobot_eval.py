"""SWE-bench evaluation adapter for Nanobot (base agent, no posture or deliberation).

Runs Nanobot agent against SWE-bench Lite instances and collects patches.
This is the baseline condition: no posture tracking, no deliberation hooks.

Usage:
    # Run 5 instances
    source /tmp/swebench_env/bin/activate && python3 scripts/swebench_nanobot_eval.py --n 5 --run-id run5_nanobot

    # Run with specific offset
    python scripts/swebench_nanobot_eval.py --n 5 --offset 0 --run-id run5_nanobot

    # Run specific instance IDs
    python scripts/swebench_nanobot_eval.py --instance-ids django__django-11039 --run-id run5_nanobot
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

# Insert nanobot vendor path so we can import from it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor" / "nanobot"))

from datasets import load_dataset
from loguru import logger

_ZAI_API_KEY = os.environ.get("ZAI_API_KEY", "")
_ZAI_API_BASE = "https://api.z.ai/api/coding/paas/v4"
_MODEL = "glm-5.1"
_MAX_ITERATIONS = 100
_RESULTS_DIR = Path(__file__).resolve().parent.parent / "benchmarks" / "swebench_5"
_REPOS_CACHE = Path("/tmp/swebench_repos")

_SYSTEM_PROMPT = """\
You are a software engineer working on fixing a bug in a Python repository.
You have access to the repository files. Your task is to:
1. Understand the issue described below
2. Find the relevant code
3. Implement a fix

Do NOT create tests. Do NOT modify test files. Focus only on fixing the source code.
When you're done, just say "DONE" — the git diff will be captured automatically.
"""

_TASK_TEMPLATE = """\
{system_prompt}

---

Repository: {repo}
Issue: {instance_id}

{problem_statement}

Fix this issue by modifying the source code. Do not modify tests.
"""


def _checkout_repo(repo: str, base_commit: str, dest: Path) -> bool:
    """Clone repo at specific commit. Uses cache for speed."""
    cache_dir = _REPOS_CACHE / repo.replace("/", "__")

    if not cache_dir.exists():
        logger.info("Cloning {} ...", repo)
        result = subprocess.run(
            ["git", "clone", "--quiet", f"https://github.com/{repo}.git", str(cache_dir)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            logger.error("Clone failed: {}", result.stderr[:200])
            return False

    # Copy and checkout the right commit
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(cache_dir, dest, symlinks=True)

    result = subprocess.run(
        ["git", "checkout", "--force", base_commit],
        capture_output=True, text=True, cwd=dest, timeout=60,
    )
    if result.returncode != 0:
        logger.error("Checkout {} failed: {}", base_commit[:8], result.stderr[:200])
        return False

    # Clean any untracked files
    subprocess.run(
        ["git", "clean", "-fd"],
        capture_output=True, cwd=dest, timeout=30,
    )
    return True


def _get_diff(workspace: Path) -> str:
    """Get the git diff of all changes made by the agent."""
    result = subprocess.run(
        ["git", "diff"],
        capture_output=True, text=True, cwd=workspace, timeout=30,
    )
    return result.stdout


def _extract_token_usage(messages: list[dict]) -> dict:
    """Extract aggregated token usage from LLM response messages."""
    total_prompt = 0
    total_completion = 0
    total_cached = 0
    for msg in messages:
        if "usage" in msg:
            u = msg["usage"]
            total_prompt += u.get("prompt_tokens", 0)
            total_completion += u.get("completion_tokens", 0)
            total_cached += u.get("cached_tokens", 0)
    return {
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "cached_tokens": total_cached,
        "total_tokens": total_prompt + total_completion,
    }


def _tool_breakdown(tools: list[str]) -> dict[str, int]:
    """Count how many times each tool was called."""
    counts: dict[str, int] = {}
    for t in tools:
        if t:
            counts[t] = counts.get(t, 0) + 1
    return counts


def _build_config(workspace: Path) -> dict:
    """Build a Nanobot config dict for this evaluation run.

    No posture, no deliberation — pure base agent.
    """
    config = {
        "agents": {
            "defaults": {
                "model": _MODEL,
                "provider": "custom",
                "max_tokens": 16384,
                "context_window_tokens": 131072,
                "temperature": 0.1,
                "max_tool_iterations": _MAX_ITERATIONS,
                "workspace": str(workspace),
            }
        },
        "providers": {
            "custom": {
                "api_key": _ZAI_API_KEY,
                "api_base": _ZAI_API_BASE,
            },
        },
    }
    return config


async def _run_instance(
    instance: dict,
    workspace: Path,
) -> dict:
    """Run Nanobot on a single SWE-bench instance. Returns prediction dict."""
    instance_id = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]

    logger.info(">>> {} (nanobot base, no posture, no deliberation)", instance_id)
    start = time.time()

    # Checkout repo
    if not _checkout_repo(repo, base_commit, workspace):
        return {
            "instance_id": instance_id,
            "model_patch": "",
            "model_name_or_path": "nanobot-glm51-base",
            "error": "checkout_failed",
        }

    # Build config and write to temp file
    config = _build_config(workspace)
    config_path = workspace / ".nanobot_eval_config.json"
    config_path.write_text(json.dumps(config, indent=2))

    # Build task with system prompt prepended
    task = _TASK_TEMPLATE.format(
        system_prompt=_SYSTEM_PROMPT,
        repo=repo,
        instance_id=instance_id,
        problem_statement=instance["problem_statement"],
    )

    session_key = f"swebench:{instance_id}"
    tools_used = []
    content = ""
    messages = []

    try:
        from nanobot.nanobot import Nanobot
        bot = Nanobot.from_config(config_path, workspace=workspace)
        result = await asyncio.wait_for(
            bot.run(task, session_key=session_key),
            timeout=600,  # 10 min max per instance
        )
        tools_used = result.tools_used
        content = result.content
        messages = result.messages
    except asyncio.TimeoutError:
        logger.warning("Timeout on {}", instance_id)
        content = "TIMEOUT"
    except Exception as e:
        logger.error("Error on {}: {}", instance_id, str(e)[:200])
        content = f"ERROR: {e}"

    # Capture the diff
    patch = _get_diff(workspace)
    elapsed = time.time() - start

    # Extract token usage from messages
    token_stats = _extract_token_usage(messages)

    logger.info("<<< {} — patch={} chars, tools={}, time={:.0f}s, tokens={}",
                instance_id, len(patch), len(tools_used), elapsed,
                token_stats.get("total_tokens", 0))

    return {
        "instance_id": instance_id,
        "model_patch": patch,
        "model_name_or_path": "nanobot-glm51-base",
        "elapsed_s": round(elapsed, 1),
        "tools_used_count": len(tools_used),
        "tools_breakdown": _tool_breakdown(tools_used),
        "iterations": len([t for t in tools_used if t]),
        "token_stats": token_stats,
        "error": None if patch else ("timeout" if "TIMEOUT" in content else "no_patch"),
    }


async def run_evaluation(
    n: int,
    run_id: str,
    offset: int = 0,
    instance_ids: list[str] | None = None,
):
    """Run Nanobot on N SWE-bench instances."""
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _REPOS_CACHE.mkdir(parents=True, exist_ok=True)

    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")

    if instance_ids:
        instances = [r for r in ds if r["instance_id"] in instance_ids]
    else:
        instances = list(ds)[offset:offset + n]

    predictions_path = _RESULTS_DIR / f"{run_id}.jsonl"
    stats_path = _RESULTS_DIR / f"{run_id}_stats.json"

    logger.info("Running {} instances (nanobot base), output={}",
                len(instances), predictions_path)

    results = []
    workspace = Path(tempfile.mkdtemp(prefix="swebench_nanobot_"))

    try:
        for i, inst in enumerate(instances):
            logger.info("[{}/{}] Starting {}", i + 1, len(instances), inst["instance_id"])
            pred = await _run_instance(inst, workspace)
            results.append(pred)

            # Append prediction incrementally
            with predictions_path.open("a") as f:
                f.write(json.dumps({
                    "instance_id": pred["instance_id"],
                    "model_patch": pred["model_patch"],
                    "model_name_or_path": pred["model_name_or_path"],
                }) + "\n")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    # Write detailed results (per-instance)
    detailed_path = _RESULTS_DIR / f"{run_id}_detailed.jsonl"
    with detailed_path.open("w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Write aggregate stats
    total = len(results)
    non_empty = sum(1 for r in results if r.get("model_patch"))
    errors = sum(1 for r in results if r.get("error"))
    avg_time = sum(r.get("elapsed_s", 0) for r in results) / max(total, 1)
    avg_tools = sum(r.get("tools_used_count", 0) for r in results) / max(total, 1)
    avg_iterations = sum(r.get("iterations", 0) for r in results) / max(total, 1)
    total_tokens = sum(r.get("token_stats", {}).get("total_tokens", 0) for r in results)

    stats = {
        "run_id": run_id,
        "condition": "nanobot_base",
        "description": "Nanobot base agent — no posture, no deliberation",
        "total_instances": total,
        "patches_generated": non_empty,
        "errors": errors,
        "avg_elapsed_s": round(avg_time, 1),
        "avg_tools_used": round(avg_tools, 1),
        "avg_iterations": round(avg_iterations, 1),
        "total_tokens": total_tokens,
        "model": _MODEL,
        "max_iterations": _MAX_ITERATIONS,
        "per_instance": [
            {
                "id": r["instance_id"],
                "patch_generated": bool(r.get("model_patch")),
                "eval_resolved": None,
                "elapsed_s": r.get("elapsed_s"),
                "iterations": r.get("iterations"),
                "tools": r.get("tools_breakdown"),
                "tokens": r.get("token_stats", {}).get("total_tokens", 0),
            }
            for r in results
        ],
    }
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    logger.info("Stats written to {}", stats_path)
    logger.info("Summary: {}/{} patches, avg {:.0f}s, avg {:.0f} iters, {} tokens total",
                non_empty, total, avg_time, avg_iterations, total_tokens)

    return predictions_path


def run_swebench_evaluation(predictions_path: Path, run_id: str) -> list[str]:
    """Run official swebench evaluation and return resolved instance IDs."""
    lock_path = Path("/tmp/swebench_eval.lock")
    logger.info("Waiting for eval lock (run_id={})...", run_id)
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        logger.info("Running SWE-bench evaluation on {}", predictions_path)
        cmd = [
            sys.executable, "-m", "swebench.harness.run_evaluation",
            "--dataset_name", "princeton-nlp/SWE-bench_Lite",
            "--split", "test",
            "--predictions_path", str(predictions_path),
            "--max_workers", "2",
            "--timeout", "300",
            "--run_id", run_id,
            "--cache_level", "base",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        print(result.stdout)
        if result.returncode != 0:
            print(result.stderr[-500:], file=sys.stderr)

    resolved_ids = _parse_eval_report(run_id)
    _update_stats_with_eval(run_id, resolved_ids)
    return resolved_ids


def _parse_eval_report(run_id: str) -> list[str]:
    """Parse SWE-bench evaluation results from per-instance report.json files."""
    resolved_ids: list[str] = []
    eval_log_dir = Path("logs/run_evaluation") / run_id
    if not eval_log_dir.exists():
        logger.warning("Eval log dir not found: {}", eval_log_dir)
        return []

    for model_dir in eval_log_dir.iterdir():
        if not model_dir.is_dir():
            continue
        for instance_dir in model_dir.iterdir():
            if not instance_dir.is_dir():
                continue
            report_file = instance_dir / "report.json"
            if not report_file.exists():
                continue
            try:
                report = json.loads(report_file.read_text())
                instance_id = instance_dir.name
                if report.get(instance_id, {}).get("resolved", False):
                    resolved_ids.append(instance_id)
            except (json.JSONDecodeError, KeyError):
                pass

    logger.info("Eval result for {}: {} resolved ({})",
                run_id, len(resolved_ids),
                ", ".join(resolved_ids) or "none")
    return resolved_ids


def _update_stats_with_eval(run_id: str, resolved_ids: list[str]) -> None:
    """Update the stats JSON with actual eval results."""
    stats_path = _RESULTS_DIR / f"{run_id}_stats.json"
    if not stats_path.exists():
        return
    stats = json.loads(stats_path.read_text())
    total = stats.get("total_instances", 0)
    for inst in stats.get("per_instance", []):
        inst["eval_resolved"] = inst["id"] in resolved_ids
    stats["eval_resolved"] = len(resolved_ids)
    stats["eval_resolve_rate"] = f"{len(resolved_ids)}/{total}" if total else "0/0"
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    logger.info("Stats updated with eval results: {} resolved", len(resolved_ids))


def main():
    parser = argparse.ArgumentParser(description="SWE-bench evaluation for Nanobot (base agent)")
    parser.add_argument("--n", type=int, default=5, help="Number of instances to run")
    parser.add_argument("--offset", type=int, default=0, help="Start from this index")
    parser.add_argument("--instance-ids", nargs="+", help="Specific instance IDs")
    parser.add_argument("--run-id", required=True, help="Unique run identifier")
    parser.add_argument("--evaluate", action="store_true", help="Only run evaluation")
    parser.add_argument("--predictions", type=Path, help="Path to predictions JSONL")
    parser.add_argument("--auto-eval", action="store_true", help="Evaluate after running")

    args = parser.parse_args()

    stamped_run_id = f"{date.today().isoformat()}_{args.run_id}"

    if args.evaluate:
        if not args.predictions:
            parser.error("--predictions required with --evaluate")
        resolved = run_swebench_evaluation(args.predictions, stamped_run_id)
        logger.info("Eval complete: {} resolved", len(resolved))
        return

    predictions_path = asyncio.run(run_evaluation(
        n=args.n,
        run_id=stamped_run_id,
        offset=args.offset,
        instance_ids=args.instance_ids,
    ))

    if args.auto_eval:
        resolved = run_swebench_evaluation(predictions_path, stamped_run_id)
        logger.info("Final: {}/{} resolved by eval harness", len(resolved), args.n)


if __name__ == "__main__":
    main()
