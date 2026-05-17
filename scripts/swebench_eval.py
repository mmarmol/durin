"""SWE-bench evaluation adapter for Durin.

Runs Durin agent (posture + plan + deliberation V3) against SWE-bench Lite.
All Durin features are active by default. Use --no-* flags to disable.

Usage:
    # Run 5 instances with all features (default)
    python scripts/swebench_eval.py --n 5 --run-id durin_full --auto-eval

    # Disable deliberation for A/B comparison
    python scripts/swebench_eval.py --n 5 --no-deliberation --run-id durin_nodelib --auto-eval

    # Evaluate existing predictions
    python scripts/swebench_eval.py --evaluate --predictions benchmarks/swebench_5/run.jsonl --run-id eval_run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datasets import load_dataset
from loguru import logger

_ZAI_API_KEY = os.environ.get("ZAI_API_KEY", "")
_ZAI_API_BASE = "https://api.z.ai/api/coding/paas/v4"
_MODEL = "glm-5.1"
_DELIB_MODEL = "glm-5.1"
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


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def _collect_telemetry(session_key: str) -> list[dict]:
    """Collect telemetry events for this session from the JSONL log."""
    import re

    telemetry_dir = Path.home() / ".cache" / "durin" / "telemetry"
    safe_key = re.sub(r"[^\w\-]", "_", session_key)[:80]
    today = date.today().isoformat()
    filename = f"{safe_key}_{today}.jsonl"
    return _read_jsonl(telemetry_dir / filename)


def _collect_plan_events(session_key: str, workspace: Path) -> list[dict]:
    """Collect plan events from the plan store for this session."""
    plan_dir = workspace / "plans" / session_key
    return _read_jsonl(plan_dir / "events.jsonl")


def _build_feature_usage(telemetry: list[dict], plan_events: list[dict], tools_breakdown: dict) -> dict:
    """Extract structured feature-usage summary from raw events."""
    # --- Plan system ---
    tier = None
    phases_seen = []
    confirm_results = []
    cycle_count = 0
    for ev in plan_events:
        etype = ev.get("type", "")
        if etype == "tier_set":
            tier = ev.get("tier")
        elif etype == "phase_transition":
            phase = ev.get("to_phase") or ev.get("phase")
            if phase:
                phases_seen.append(phase)
        elif etype == "confirm_result":
            confirm_results.append(ev.get("result", "unknown"))
        elif etype == "cycle_restart":
            cycle_count += 1

    plan_usage = {
        "tier": tier,
        "cycles": max(cycle_count + 1, 1) if tier == "full_plan" else 0,
        "phases": phases_seen,
        "phase_transitions": len(phases_seen),
        "confirm_results": confirm_results,
    }

    # --- Deliberation V3 ---
    delib_events = [e for e in telemetry if e.get("type") == "deliberation.result"]
    deliberation_usage = {
        "count": len(delib_events),
        "total_ms": sum(e.get("data", {}).get("duration_ms", 0) for e in delib_events),
        "triggers": [e.get("data", {}).get("trigger", "") for e in delib_events],
        "perspectives": [
            {
                "trigger": e.get("data", {}).get("trigger", ""),
                "cycle": e.get("data", {}).get("cycle", 1),
                "critic": e.get("data", {}).get("perspectives", {}).get("critic", "")[:150],
                "explorer": e.get("data", {}).get("perspectives", {}).get("explorer", "")[:150],
                "pragmatic": e.get("data", {}).get("perspectives", {}).get("pragmatic", "")[:150],
                "synthesis": e.get("data", {}).get("synthesis", "")[:200],
            }
            for e in delib_events
        ],
    }

    # --- Posture evolution ---
    posture_changes = [e for e in telemetry if e.get("type") == "posture.change"]
    posture_initial_ev = [e for e in telemetry if e.get("type") == "posture.initial"]
    all_stimulus_events = []
    caution_values = []
    for ev in posture_changes:
        data = ev.get("data", {})
        all_stimulus_events.extend(data.get("stimulus_events", []))
        caution_val = data.get("axes", {}).get("caution")
        if caution_val is not None:
            caution_values.append(caution_val)

    posture_usage = {
        "initial": posture_initial_ev[0].get("data", {}).get("axes", {}) if posture_initial_ev else {},
        "total_changes": len(posture_changes),
        "stimulus_events_fired": list(set(all_stimulus_events)),
        "stimulus_event_counts": _count_items(all_stimulus_events),
        "caution_range": [round(min(caution_values), 3), round(max(caution_values), 3)] if caution_values else [],
    }

    # --- Verification ---
    has_confirm = any(r in ("pass", "fail") for r in confirm_results)
    verify_caught = "fail" in confirm_results
    verification_usage = {
        "tier_supports_verify": tier in ("execute_verify", "full_plan"),
        "confirm_executed": has_confirm,
        "caught_issue": verify_caught,
    }

    return {
        "plan": plan_usage,
        "deliberation": deliberation_usage,
        "posture": posture_usage,
        "verification": verification_usage,
        "tools": tools_breakdown,
    }


def _count_items(items: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts




def _tool_breakdown(tools: list[str]) -> dict[str, int]:
    """Count how many times each tool was called."""
    counts: dict[str, int] = {}
    for t in tools:
        if t:
            counts[t] = counts.get(t, 0) + 1
    return counts


def _build_config(
    workspace: Path,
    deliberation: bool,
    posture_override: dict[str, float] | None = None,
) -> dict:
    """Build a Durin config dict for this evaluation run.

    All features (posture, plan, deliberation) active by default.
    """
    posture_cfg: dict = {"enabled": True}
    if posture_override:
        axes = {}
        axis_defaults = {
            "caution": {"mean": 0.6, "variance": 0.15, "return_force": 0.3},
            "exploration": {"mean": 0.4, "variance": 0.20, "return_force": 0.4},
            "depth": {"mean": 0.5, "variance": 0.20, "return_force": 0.5},
            "discipline": {"mean": 0.5, "variance": 0.15, "return_force": 0.2},
            "conformity": {"mean": 0.7, "variance": 0.15, "return_force": 0.3},
        }
        for axis_name, defaults in axis_defaults.items():
            val = posture_override.get(axis_name, defaults["mean"])
            axes[axis_name] = {
                "mean": defaults["mean"],
                "variance": defaults["variance"],
                "return_force": defaults["return_force"],
                "current_value": val,
            }
        posture_cfg["axes"] = axes

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
                "posture": posture_cfg,
                "plan": {"enabled": True},
                "deliberation": {
                    "enabled": deliberation,
                    "provider": "custom",
                    "model": _DELIB_MODEL,
                },
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
    deliberation: bool,
    workspace: Path,
    run_id: str,
    carry_posture: dict[str, float] | None = None,
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
    config = _build_config(workspace, deliberation, posture_override=carry_posture)
    config_path = workspace / ".durin_eval_config.json"
    config_path.write_text(json.dumps(config, indent=2))

    # Run Durin SDK
    task = _TASK_TEMPLATE.format(
        repo=repo,
        instance_id=instance_id,
        problem_statement=instance["problem_statement"],
    )

    session_key = f"swebench:{run_id}:{instance_id}"
    tools_used = []
    content = ""
    messages = []
    posture_final = {}

    usage_totals: dict[str, int] = {}

    try:
        from durin.durin_sdk import Durin
        bot = Durin.from_config(config_path, workspace=workspace, session_key=session_key)
        result = await asyncio.wait_for(
            bot.run(task, session_key=session_key),
            timeout=600,  # 10 min max per instance
        )
        tools_used = result.tools_used
        content = result.content
        messages = result.messages
        usage_totals = result.usage or {}
        # Extract final posture state from hooks
        for hook in (bot._loop._extra_hooks or []):
            if hasattr(hook, "current_vector"):
                posture_final = hook.current_vector.snapshot()
                break
    except asyncio.TimeoutError:
        logger.warning("Timeout on {}", instance_id)
        content = "TIMEOUT"
    except Exception as e:
        logger.error("Error on {}: {}", instance_id, str(e)[:200])
        content = f"ERROR: {e}"

    # Capture the diff
    patch = _get_diff(workspace)
    elapsed = time.time() - start

    # Collect telemetry (posture + deliberation events)
    telemetry_events = _collect_telemetry(session_key)
    plan_events = _collect_plan_events(session_key, workspace)

    # Token usage from runner's accumulated totals
    token_stats = {
        "prompt_tokens": usage_totals.get("prompt_tokens", 0),
        "completion_tokens": usage_totals.get("completion_tokens", 0),
        "cached_tokens": usage_totals.get("cached_tokens", 0),
        "total_tokens": usage_totals.get("prompt_tokens", 0) + usage_totals.get("completion_tokens", 0),
    }

    tools_breakdown = _tool_breakdown(tools_used)
    feature_usage = _build_feature_usage(telemetry_events, plan_events, tools_breakdown)

    logger.info("<<< {} — patch={} chars, tier={}, delib={}, time={:.0f}s, tokens={}",
                instance_id, len(patch), feature_usage["plan"]["tier"],
                feature_usage["deliberation"]["count"], elapsed,
                token_stats.get("total_tokens", 0))

    return {
        "instance_id": instance_id,
        "model_patch": patch,
        "model_name_or_path": f"durin-glm51-{'delib' if deliberation else 'nodelib'}",
        "elapsed_s": round(elapsed, 1),
        "iterations": len([t for t in tools_used if t]),
        "tools_used_count": len(tools_used),
        "token_stats": token_stats,
        "feature_usage": feature_usage,
        "posture_final": {k: round(v, 4) for k, v in posture_final.items()},
        "telemetry_raw": telemetry_events,
        "plan_events_raw": plan_events,
        "error": None if patch else ("timeout" if "TIMEOUT" in content else "no_patch"),
    }


async def run_evaluation(
    n: int,
    deliberation: bool,
    run_id: str,
    offset: int = 0,
    instance_ids: list[str] | None = None,
    carry_posture: bool = False,
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

    logger.info("Running {} instances, deliberation={}, carry_posture={}, output={}",
                len(instances), deliberation, carry_posture, predictions_path)

    results = []
    workspace = Path(tempfile.mkdtemp(prefix="swebench_work_"))
    current_posture: dict[str, float] | None = None

    try:
        for i, inst in enumerate(instances):
            logger.info("[{}/{}] Starting {}", i + 1, len(instances), inst["instance_id"])
            posture_input = current_posture if carry_posture else None
            pred = await _run_instance(inst, deliberation, workspace, run_id=run_id, carry_posture=posture_input)
            results.append(pred)

            # Carry forward posture to next instance
            if carry_posture and pred.get("posture_final"):
                current_posture = pred["posture_final"]
                logger.info("Carrying posture: {}", {k: f"{v:.3f}" for k, v in current_posture.items()})

            # Append prediction incrementally
            with predictions_path.open("a") as f:
                f.write(json.dumps({
                    "instance_id": pred["instance_id"],
                    "model_patch": pred["model_patch"],
                    "model_name_or_path": pred["model_name_or_path"],
                }) + "\n")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    # Write detailed results (per-instance with telemetry)
    detailed_path = _RESULTS_DIR / f"{run_id}_detailed.jsonl"
    with detailed_path.open("w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Write aggregate stats
    total = len(results)
    non_empty = sum(1 for r in results if r.get("model_patch"))
    errors = sum(1 for r in results if r.get("error"))
    avg_time = sum(r.get("elapsed_s", 0) for r in results) / max(total, 1)
    avg_iterations = sum(r.get("iterations", 0) for r in results) / max(total, 1)
    total_prompt = sum(r.get("token_stats", {}).get("prompt_tokens", 0) for r in results)
    total_completion = sum(r.get("token_stats", {}).get("completion_tokens", 0) for r in results)
    total_cached = sum(r.get("token_stats", {}).get("cached_tokens", 0) for r in results)
    total_tokens = total_prompt + total_completion

    # Feature adoption summary across all instances
    tiers_used = [r.get("feature_usage", {}).get("plan", {}).get("tier") for r in results]
    delib_counts = [r.get("feature_usage", {}).get("deliberation", {}).get("count", 0) for r in results]
    verify_caught = sum(
        1 for r in results
        if r.get("feature_usage", {}).get("verification", {}).get("caught_issue")
    )

    stats = {
        "run_id": run_id,
        "config": {
            "deliberation_enabled": deliberation,
            "carry_posture": carry_posture,
            "model": _MODEL,
            "delib_model": _DELIB_MODEL,
            "max_iterations": _MAX_ITERATIONS,
        },
        "summary": {
            "total_instances": total,
            "patches_generated": non_empty,
            "errors": errors,
            "avg_elapsed_s": round(avg_time, 1),
            "avg_iterations": round(avg_iterations, 1),
            "tokens": {
                "total": total_tokens,
                "prompt": total_prompt,
                "completion": total_completion,
                "cached": total_cached,
            },
        },
        "feature_adoption": {
            "tiers": _count_items([t for t in tiers_used if t]),
            "deliberations_total": sum(delib_counts),
            "instances_with_deliberation": sum(1 for c in delib_counts if c > 0),
            "verify_caught_issues": verify_caught,
        },
        "per_instance": [
            {
                "id": r["instance_id"],
                "patch_generated": bool(r.get("model_patch")),
                "eval_resolved": None,
                "elapsed_s": r.get("elapsed_s"),
                "iterations": r.get("iterations"),
                "tools_used": r.get("tools_used_count", 0),
                "tokens": r.get("token_stats", {}),
                "feature_usage": r.get("feature_usage", {}),
                "posture_final": r.get("posture_final", {}),
                "error": r.get("error"),
            }
            for r in results
        ],
    }
    stats_path = _RESULTS_DIR / f"{run_id}_stats.json"
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    logger.info("Stats written to {}", stats_path)
    logger.info("Summary: {}/{} patches generated (eval pending), avg {:.0f}s, avg {:.0f} iters, {} tokens",
                non_empty, total, avg_time, avg_iterations, total_tokens)
    logger.info("Feature adoption: tiers={}, delib={}, verify_caught={}",
                _count_items([t for t in tiers_used if t]), sum(delib_counts), verify_caught)

    return predictions_path


def run_swebench_evaluation(predictions_path: Path, run_id: str) -> list[str]:
    """Run official swebench evaluation and return resolved instance IDs.

    Uses a file lock to prevent parallel eval runs from interfering with
    each other (Docker/harness shared state).
    """
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
    """Parse SWE-bench evaluation results from per-instance report.json files.

    Only reads from the exact run_id directory to avoid picking up stale
    results from prior runs with similar names.
    """
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
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to parse {}: {}", report_file, e)

    logger.info("Eval result for {}: {}/{} resolved ({})",
                run_id, len(resolved_ids),
                sum(1 for _ in eval_log_dir.rglob("report.json")),
                ", ".join(resolved_ids) or "none")
    return resolved_ids


def _update_stats_with_eval(run_id: str, resolved_ids: list[str]) -> None:
    """Update the stats JSON with actual eval results."""
    stats_path = _RESULTS_DIR / f"{run_id}_stats.json"
    if not stats_path.exists():
        return

    stats = json.loads(stats_path.read_text())
    total = stats.get("summary", {}).get("total_instances", 0)

    for inst in stats.get("per_instance", []):
        inst["eval_resolved"] = inst["id"] in resolved_ids

    stats["summary"]["eval_resolved"] = len(resolved_ids)
    stats["summary"]["eval_resolve_rate"] = (
        f"{len(resolved_ids)}/{total}" if total else "0/0"
    )

    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    logger.info("Stats updated with eval results: {} resolved", len(resolved_ids))


def main():
    parser = argparse.ArgumentParser(description="SWE-bench evaluation for Durin")
    parser.add_argument("--n", type=int, default=5, help="Number of instances to run")
    parser.add_argument("--offset", type=int, default=0, help="Start from this index")
    parser.add_argument("--instance-ids", nargs="+", help="Specific instance IDs")
    parser.add_argument("--no-deliberation", action="store_true",
                        help="Disable deliberation (active by default)")
    parser.add_argument("--run-id", required=True, help="Unique run identifier")
    parser.add_argument("--carry-posture", action="store_true",
                        help="Carry posture state between instances")
    parser.add_argument("--evaluate", action="store_true", help="Only run evaluation")
    parser.add_argument("--predictions", type=Path, help="Path to predictions JSONL")
    parser.add_argument("--auto-eval", action="store_true", help="Evaluate after running")

    args = parser.parse_args()

    if args.evaluate:
        if not args.predictions:
            parser.error("--predictions required with --evaluate")
        resolved = run_swebench_evaluation(args.predictions, args.run_id or "eval")
        logger.info("Eval complete: {} resolved", len(resolved))
        return

    deliberation = not args.no_deliberation
    stamped_run_id = f"{date.today().isoformat()}_{args.run_id}"

    predictions_path = asyncio.run(run_evaluation(
        n=args.n,
        deliberation=deliberation,
        run_id=stamped_run_id,
        offset=args.offset,
        instance_ids=args.instance_ids,
        carry_posture=args.carry_posture,
    ))

    if args.auto_eval:
        resolved = run_swebench_evaluation(predictions_path, stamped_run_id)
        logger.info("Final result: {}/{} instances resolved by eval harness",
                    len(resolved), args.n)


if __name__ == "__main__":
    main()
