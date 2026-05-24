"""Single-QA replay from a saved trace.

Re-runs ONE QA against the current durin code so you can iterate:
fix a bug → replay → compare new answer vs the one captured in the
trace. Same harness as ``locomo_run``, same telemetry binding —
results overwrite the trace + telemetry in the original run dir.

Usage:

    python -m scripts.benchmark.locomo_replay \\
        --data-path ~/.cache/durin/locomo10.json \\
        bench-results/locomo/<run>/traces/<qa_id>.json

The data path is required because traces don't carry the full
conversation transcript (would inflate disk by 10x for nothing —
the conv lives in the dataset). We look the QA up by ``qa_id`` and
re-seed memory from the same conversation.

After replay:

- The original trace.json is REPLACED with the new run's output.
- The original telemetry.jsonl is REPLACED.
- A ``previous.json`` sidecar is written so you can diff before/after.

The verdict is regenerated too — re-judge the new answer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def _replay_async(args: argparse.Namespace) -> int:
    from scripts.benchmark.locomo_dataset import LoCoMoDatasetError, load_dataset
    from scripts.benchmark.locomo_harness import run_qa
    from scripts.benchmark.locomo_judge import JudgeError, judge_answer

    trace_path = Path(args.trace_path).resolve()
    if not trace_path.is_file():
        print(f"[locomo_replay] trace not found: {trace_path}", file=sys.stderr)
        return 2
    try:
        prev = json.loads(trace_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[locomo_replay] malformed trace: {exc}", file=sys.stderr)
        return 2

    qa_id = prev.get("qa_id")
    if not qa_id:
        print(f"[locomo_replay] trace has no qa_id", file=sys.stderr)
        return 2

    try:
        all_qas = load_dataset(args.data_path)
    except LoCoMoDatasetError as exc:
        print(f"[locomo_replay] {exc}", file=sys.stderr)
        return 2
    qa = next((q for q in all_qas if q.qa_id == qa_id), None)
    if qa is None:
        print(f"[locomo_replay] dataset doesn't contain qa_id={qa_id}",
              file=sys.stderr)
        return 2

    run_dir = trace_path.parent.parent  # …/run/traces/<qa>.json → …/run/
    telemetry_path = run_dir / "telemetry" / f"{qa_id}.jsonl"
    workspace = run_dir / "workspaces" / qa_id

    # Stash prior trace + telemetry as <name>.previous so the diff
    # remains visible after the replay. Keeps the last 2 versions only
    # so the dir doesn't grow forever.
    prev_path = trace_path.with_suffix(".previous.json")
    shutil.copy2(trace_path, prev_path)
    if telemetry_path.is_file():
        shutil.copy2(telemetry_path, telemetry_path.with_suffix(".previous.jsonl"))

    print(f"[locomo_replay] {qa_id} [{qa.category}] re-running…")
    trace = await run_qa(
        qa,
        workspace_root=workspace,
        telemetry_path=telemetry_path,
        model=args.model,
        max_iterations=args.max_iterations,
        timeout_s=args.timeout_s,
        enable_memory=not args.no_memory,
    )

    verdict_dict: dict[str, Any] = {
        "score": 0.0, "confidence": 0, "reasoning": "",
        "judge_model": args.judge_model,
    }
    try:
        from durin.memory.dream import default_llm_invoke

        v = judge_answer(
            qa.question, qa.answer, trace.got,
            llm_invoke=default_llm_invoke,
            model=args.judge_model,
        )
        verdict_dict.update({
            "score": v.score, "confidence": v.confidence, "reasoning": v.reasoning,
        })
    except JudgeError as exc:
        verdict_dict["reasoning"] = f"(judge failed) {exc}"
        verdict_dict["error"] = True

    trace_dict = trace.to_dict()
    trace_dict["verdict"] = verdict_dict
    trace_dict["replayed_at"] = datetime.now(timezone.utc).isoformat()
    trace_path.write_text(
        json.dumps(trace_dict, indent=2, ensure_ascii=False), encoding="utf-8",
    )

    prev_score = (prev.get("verdict") or {}).get("score")
    new_score = verdict_dict["score"]
    delta = "no change"
    if prev_score is not None and new_score != prev_score:
        delta = f"{prev_score} → {new_score}"
    print(f"[locomo_replay] verdict: {new_score} ({delta})")
    print(f"[locomo_replay] reasoning: {verdict_dict['reasoning'][:120]}")
    print(f"[locomo_replay] prior saved at: {prev_path.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="locomo_replay", description=__doc__)
    parser.add_argument("trace_path", help="Path to a trace .json from a prior run.")
    parser.add_argument("--data-path", required=True,
                        help="LoCoMo JSON dataset path.")
    parser.add_argument("--model", default="glm-5-turbo")
    parser.add_argument("--judge-model", default="glm-5-turbo")
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument("--timeout-s", type=float, default=90.0)
    parser.add_argument("--no-memory", action="store_true",
                        help="Replay without memory seeding (ablation baseline).")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    return asyncio.run(_replay_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
