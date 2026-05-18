"""SWE-bench agent runner — executes INSIDE a Docker container.

This script is the entrypoint for Durin running inside a SWE-bench container.
The container has:
  - /testbed: repo checked out at the correct commit (from instance image)
  - /opt/durin: agent code mounted read-only
  - /output: writable volume for results (patch, telemetry, logs, stats)
  - conda env 'testbed': project deps (untouched by agent)
  - conda env 'durin': agent deps (isolated)

The agent process runs in the 'durin' conda env.
The agent's exec tool runs commands in the 'testbed' env via subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, "/opt/durin")

from loguru import logger

_OUTPUT_DIR = Path("/output")
_WORKSPACE = Path("/testbed")

_EXEC_PREFIX = (
    "source /opt/miniconda3/bin/activate && "
    "conda activate testbed && "
    "cd /testbed && "
)

_SYSTEM_PROMPT = """\
You are a software engineer working on fixing a bug in a Python repository.
You have access to the repository files at /testbed. Your task is to:
1. Understand the issue described below
2. Find the relevant code
3. Implement a fix

Do NOT create tests. Do NOT modify test files. Focus only on fixing the source code.
When you're done, call complete_goal with a recap of the fix.

IMPORTANT: All shell commands run inside the project environment at /testbed.
You can run pytest directly — the environment has all dependencies installed.
"""

_TASK_TEMPLATE = """\
Repository: {repo}
Issue: {instance_id}

{problem_statement}

Fix this issue by modifying the source code. Do not modify tests.
"""


def _build_config(
    api_key: str,
    api_base: str,
    model: str,
    deliberation: bool,
    max_iterations: int,
) -> dict:
    """Build Durin config for inside-container execution."""
    return {
        "agents": {
            "defaults": {
                "model": model,
                "provider": "custom",
                "max_tokens": 16384,
                "context_window_tokens": 131072,
                "temperature": 0.1,
                "max_tool_iterations": max_iterations,
                "workspace": str(_WORKSPACE),
                "restrict_to_workspace": True,
                "posture": {"enabled": True},
                "plan": {"enabled": True},
                "exec": {
                    "enable": True,
                    "timeout": 300,
                    "sandbox": "testbed",
                },
                "deliberation": {
                    "enabled": deliberation,
                    "provider": "custom",
                    "model": model,
                },
            }
        },
        "providers": {
            "custom": {
                "api_key": api_key,
                "api_base": api_base,
            },
        },
    }


def _get_diff() -> str:
    """Get git diff of changes made by the agent."""
    result = subprocess.run(
        ["git", "diff"],
        capture_output=True, text=True, cwd=_WORKSPACE, timeout=30,
    )
    return result.stdout


def _collect_telemetry(session_key: str) -> list[dict]:
    """Collect telemetry events for this session."""
    import re

    telemetry_dir = Path.home() / ".cache" / "durin" / "telemetry"
    safe_key = re.sub(r"[^\w\-]", "_", session_key)[:80]
    today = date.today().isoformat()
    filename = f"{safe_key}_{today}.jsonl"
    path = telemetry_dir / filename
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


def _collect_plan_events(session_key: str) -> list[dict]:
    """Collect plan events from the plan store."""
    plan_dir = _WORKSPACE / "plans" / session_key
    events_file = plan_dir / "events.jsonl"
    if not events_file.exists():
        return []
    events = []
    with events_file.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def _build_nanobot_config(
    api_key: str,
    api_base: str,
    model: str,
    max_iterations: int,
) -> dict:
    """Build config for base nanobot (no posture, no plan, no deliberation)."""
    return {
        "agents": {
            "defaults": {
                "model": model,
                "provider": "custom",
                "max_tokens": 16384,
                "context_window_tokens": 131072,
                "temperature": 0.1,
                "max_tool_iterations": max_iterations,
                "workspace": str(_WORKSPACE),
                "restrict_to_workspace": True,
                "posture": {"enabled": False},
                "plan": {"enabled": False},
                "exec": {
                    "enable": True,
                    "timeout": 300,
                    "sandbox": "testbed",
                },
                "deliberation": {"enabled": False},
            }
        },
        "providers": {
            "custom": {
                "api_key": api_key,
                "api_base": api_base,
            },
        },
    }


async def run_agent(
    instance_id: str,
    repo: str,
    problem_statement: str,
    api_key: str,
    api_base: str,
    model: str,
    deliberation: bool,
    max_iterations: int,
    run_id: str,
    agent: str = "durin",
) -> dict:
    """Run agent on the instance. Supports 'durin' and 'nanobot'."""
    session_key = f"swebench:{run_id}:{instance_id}"
    start = time.time()

    if agent == "nanobot":
        config = _build_nanobot_config(api_key, api_base, model, max_iterations)
    else:
        config = _build_config(api_key, api_base, model, deliberation, max_iterations)

    config_path = _WORKSPACE / ".durin_eval_config.json"
    config_path.write_text(json.dumps(config, indent=2))

    task = _TASK_TEMPLATE.format(
        repo=repo,
        instance_id=instance_id,
        problem_statement=problem_statement,
    )

    tools_used = []
    posture_final = {}
    usage_totals: dict[str, int] = {}

    try:
        from durin.agent.tools.sandbox import register_testbed_prefix
        register_testbed_prefix(_EXEC_PREFIX)

        from durin.durin_sdk import Durin
        bot = Durin.from_config(
            config_path, workspace=_WORKSPACE, session_key=session_key
        )

        result = await asyncio.wait_for(
            bot.run(task, session_key=session_key),
            timeout=900,
        )
        tools_used = result.tools_used
        usage_totals = getattr(result, "usage", None) or {}
        for hook in (bot._loop._extra_hooks or []):
            if hasattr(hook, "current_vector"):
                posture_final = hook.current_vector.snapshot()
                break
    except asyncio.TimeoutError:
        logger.warning("Timeout on {}", instance_id)
    except Exception as e:
        logger.error("Error on {}: {}", instance_id, str(e)[:300])

    patch = _get_diff()
    elapsed = time.time() - start
    telemetry = _collect_telemetry(session_key)
    plan_events = _collect_plan_events(session_key)

    return {
        "instance_id": instance_id,
        "model_patch": patch,
        "model_name_or_path": f"{agent}-{model}",
        "elapsed_s": round(elapsed, 1),
        "iterations": len([t for t in tools_used if t]),
        "tools_used": tools_used,
        "token_stats": {
            "prompt_tokens": usage_totals.get("prompt_tokens", 0),
            "completion_tokens": usage_totals.get("completion_tokens", 0),
            "cached_tokens": usage_totals.get("cached_tokens", 0),
        },
        "posture_final": {k: round(v, 4) for k, v in posture_final.items()},
        "telemetry": telemetry,
        "plan_events": plan_events,
        "error": None if patch else "no_patch",
    }


def main():
    parser = argparse.ArgumentParser(description="Run Durin inside SWE-bench container")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--problem-statement", required=True,
                        help="Path to file with problem statement")
    parser.add_argument("--api-key", default=os.environ.get("ZAI_API_KEY", ""))
    parser.add_argument("--api-base", default=os.environ.get("ZAI_API_BASE",
                        "https://api.z.ai/api/coding/paas/v4"))
    parser.add_argument("--model", default="glm-5.1")
    parser.add_argument("--no-deliberation", action="store_true")
    parser.add_argument("--max-iterations", type=int, default=100)
    parser.add_argument("--run-id", default="eval")
    parser.add_argument("--agent", choices=["durin", "nanobot"], default="durin",
                        help="Which agent to run (durin=full features, nanobot=base)")

    args = parser.parse_args()

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    problem_path = Path(args.problem_statement)
    if problem_path.exists():
        problem_text = problem_path.read_text()
    else:
        problem_text = args.problem_statement

    logger.add(_OUTPUT_DIR / "agent.log", rotation="50 MB")
    logger.info("Starting {} eval for {} (docker-internal)", args.agent, args.instance_id)

    result = asyncio.run(run_agent(
        instance_id=args.instance_id,
        repo=args.repo,
        problem_statement=problem_text,
        api_key=args.api_key,
        api_base=args.api_base,
        model=args.model,
        deliberation=not args.no_deliberation,
        max_iterations=args.max_iterations,
        run_id=args.run_id,
        agent=args.agent,
    ))

    (_OUTPUT_DIR / "patch.diff").write_text(result["model_patch"])
    (_OUTPUT_DIR / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False)
    )
    (_OUTPUT_DIR / "telemetry.jsonl").write_text(
        "\n".join(json.dumps(e) for e in result.get("telemetry", []))
    )

    logger.info(
        "Done: {} — patch={} chars, iters={}, time={:.0f}s",
        args.instance_id, len(result["model_patch"]),
        result["iterations"], result["elapsed_s"],
    )


if __name__ == "__main__":
    main()
