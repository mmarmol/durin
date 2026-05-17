"""SWE-bench evaluation adapter for Durin.

Runs Durin agent against SWE-bench Lite instances and collects patches.
Two conditions: deliberation ON vs OFF, both using GLM-5.1 via Z.ai API.

Usage:
    # Run 5 instances without deliberation
    python scripts/swebench_eval.py --n 5 --no-deliberation --run-id durin_nodelib

    # Run 5 instances WITH deliberation
    python scripts/swebench_eval.py --n 5 --deliberation --run-id durin_delib

    # Evaluate collected predictions
    python scripts/swebench_eval.py --evaluate --predictions /tmp/swebench_durin/durin_nodelib.jsonl

    # Full pipeline: run + evaluate
    python scripts/swebench_eval.py --n 30 --deliberation --run-id durin_delib --auto-eval
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datasets import load_dataset
from loguru import logger

_ZAI_API_KEY = "5d5a9cbccdbf4b618e2030d0aea21a75.7qgkQN4tNqDtcyGl"
_ZAI_API_BASE = "https://api.z.ai/api/coding/paas/v4"
_MODEL = "glm-5.1"
_DELIB_MODEL = "glm-5-turbo"
_MAX_ITERATIONS = 100
_RESULTS_DIR = Path("/tmp/swebench_durin")
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


def _build_config(workspace: Path, deliberation: bool) -> dict:
    """Build a Durin config dict for this evaluation run."""
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
                "posture": {"enabled": deliberation},
                "deliberation": {
                    "enabled": deliberation,
                    "provider": "custom_delib",
                    "max_rounds": 3,
                },
            }
        },
        "providers": {
            "custom": {
                "api_key": _ZAI_API_KEY,
                "api_base": _ZAI_API_BASE,
            },
            "custom_delib": {
                "api_key": _ZAI_API_KEY,
                "api_base": _ZAI_API_BASE,
            },
        },
    }
    return config


async def _run_instance(
    instance: dict,
    deliberation: bool,
    workspace: Path,
) -> dict:
    """Run Durin on a single SWE-bench instance. Returns prediction dict."""
    instance_id = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]

    logger.info(">>> {} (delib={})", instance_id, deliberation)
    start = time.time()

    # Checkout repo
    if not _checkout_repo(repo, base_commit, workspace):
        return {
            "instance_id": instance_id,
            "model_patch": "",
            "model_name_or_path": f"durin-glm51-{'delib' if deliberation else 'nodelib'}",
            "error": "checkout_failed",
        }

    # Build config and write to temp file
    config = _build_config(workspace, deliberation)
    config_path = workspace / ".durin_eval_config.json"
    config_path.write_text(json.dumps(config, indent=2))

    # Run Durin SDK
    task = _TASK_TEMPLATE.format(
        repo=repo,
        instance_id=instance_id,
        problem_statement=instance["problem_statement"],
    )

    try:
        from durin.durin_sdk import Durin
        bot = Durin.from_config(config_path, workspace=workspace)
        result = await asyncio.wait_for(
            bot.run(task, session_key=f"swebench:{instance_id}"),
            timeout=600,  # 10 min max per instance
        )
        tools_used = result.tools_used
        content = result.content
    except asyncio.TimeoutError:
        logger.warning("Timeout on {}", instance_id)
        tools_used = []
        content = "TIMEOUT"
    except Exception as e:
        logger.error("Error on {}: {}", instance_id, str(e)[:200])
        tools_used = []
        content = f"ERROR: {e}"

    # Capture the diff
    patch = _get_diff(workspace)
    elapsed = time.time() - start

    logger.info("<<< {} — patch={} chars, tools={}, time={:.0f}s",
                instance_id, len(patch), len(tools_used), elapsed)

    return {
        "instance_id": instance_id,
        "model_patch": patch,
        "model_name_or_path": f"durin-glm51-{'delib' if deliberation else 'nodelib'}",
        "elapsed_s": round(elapsed, 1),
        "tools_used_count": len(tools_used),
        "iterations": len([t for t in tools_used if t]),
    }


async def run_evaluation(
    n: int,
    deliberation: bool,
    run_id: str,
    offset: int = 0,
    instance_ids: list[str] | None = None,
):
    """Run Durin on N SWE-bench instances."""
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _REPOS_CACHE.mkdir(parents=True, exist_ok=True)

    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")

    if instance_ids:
        instances = [r for r in ds if r["instance_id"] in instance_ids]
    else:
        instances = list(ds)[offset:offset + n]

    predictions_path = _RESULTS_DIR / f"{run_id}.jsonl"
    stats_path = _RESULTS_DIR / f"{run_id}_stats.json"

    logger.info("Running {} instances, deliberation={}, output={}",
                len(instances), deliberation, predictions_path)

    results = []
    workspace = Path(tempfile.mkdtemp(prefix="swebench_work_"))

    try:
        for i, inst in enumerate(instances):
            logger.info("[{}/{}] Starting {}", i + 1, len(instances), inst["instance_id"])
            pred = await _run_instance(inst, deliberation, workspace)
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

    # Write stats
    total = len(results)
    non_empty = sum(1 for r in results if r.get("model_patch"))
    errors = sum(1 for r in results if r.get("error"))
    avg_time = sum(r.get("elapsed_s", 0) for r in results) / max(total, 1)
    avg_tools = sum(r.get("tools_used_count", 0) for r in results) / max(total, 1)

    stats = {
        "run_id": run_id,
        "total_instances": total,
        "patches_generated": non_empty,
        "errors": errors,
        "avg_elapsed_s": round(avg_time, 1),
        "avg_tools_used": round(avg_tools, 1),
        "deliberation": deliberation,
        "model": _MODEL,
        "max_iterations": _MAX_ITERATIONS,
    }
    stats_path.write_text(json.dumps(stats, indent=2))
    logger.info("Stats: {}", json.dumps(stats, indent=2))

    return predictions_path


def run_swebench_evaluation(predictions_path: Path, run_id: str):
    """Run official swebench evaluation on collected predictions."""
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


def main():
    parser = argparse.ArgumentParser(description="SWE-bench evaluation for Durin")
    parser.add_argument("--n", type=int, default=5, help="Number of instances to run")
    parser.add_argument("--offset", type=int, default=0, help="Start from this index")
    parser.add_argument("--instance-ids", nargs="+", help="Specific instance IDs")
    parser.add_argument("--deliberation", action="store_true", help="Enable deliberation")
    parser.add_argument("--no-deliberation", action="store_true", help="Disable deliberation")
    parser.add_argument("--run-id", required=True, help="Unique run identifier")
    parser.add_argument("--evaluate", action="store_true", help="Only run evaluation")
    parser.add_argument("--predictions", type=Path, help="Path to predictions JSONL")
    parser.add_argument("--auto-eval", action="store_true", help="Evaluate after running")

    args = parser.parse_args()

    if args.evaluate:
        if not args.predictions:
            parser.error("--predictions required with --evaluate")
        run_swebench_evaluation(args.predictions, args.run_id)
        return

    deliberation = args.deliberation and not args.no_deliberation

    predictions_path = asyncio.run(run_evaluation(
        n=args.n,
        deliberation=deliberation,
        run_id=args.run_id,
        offset=args.offset,
        instance_ids=args.instance_ids,
    ))

    if args.auto_eval:
        run_swebench_evaluation(predictions_path, args.run_id)


if __name__ == "__main__":
    main()
