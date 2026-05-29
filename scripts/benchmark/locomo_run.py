"""LoCoMo benchmark runner — entry point invoked from the CLI.

Usage:

    python -m scripts.benchmark.locomo_run \\
        --data-path ~/.cache/durin/locomo10.json \\
        --per-category 5 \\
        --model glm-5.1 \\
        --judge-model glm-5.1

Behaviour:

- Creates ``bench-results/locomo/<YYYY-MM-DD>_<commit_sha8>/`` (so we
  always know which commit a result corresponds to).
- Writes ``manifest.json`` with run config + commit + ts before any
  QA runs (so a partial run is still traceable).
- For each QA: harness → judge → persist trace → next.
- ``--resume`` skips QAs that already have a non-empty trace + verdict
  in the target run dir (idempotent re-invocations).
- Runs ``locomo_analyze.analyze_run`` at the end to categorise
  failures + generate per-failure markdown + write summary.json.

Cost model: ~2 LLM calls per QA (agent answer + judge). With glm-5.1
on a z.ai coding plan subscription that's zero marginal cost. For a
25-QA stratified run: ~50 calls total, ~30–40 min wall-clock.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _project_root() -> Path:
    """Resolve the repo root so we write into bench-results/ regardless
    of where the script is invoked from."""
    return Path(__file__).resolve().parent.parent.parent


def _git_commit_sha() -> str:
    """Best-effort current commit SHA. ``unknown`` when not in a git
    work tree (e.g. running from a sdist install)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_project_root()),
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _new_run_dir(model: str, *, no_memory: bool = False) -> Path:
    """``bench-results/locomo/<YYYY-MM-DD>_<commit8>[_nomem]/``.

    ``_nomem`` suffix distinguishes ablation runs from memory-enabled
    runs so the two can coexist in the same directory without confusion.
    """
    sha = _git_commit_sha()
    sha_short = sha[:8] if sha != "unknown" else "nogit"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    suffix = "_nomem" if no_memory else ""
    rel = f"bench-results/locomo/{stamp}_{sha_short}{suffix}"
    out = _project_root() / rel
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_manifest(
    run_dir: Path, *, args: argparse.Namespace, subset_size: int,
) -> None:
    """Write the manifest BEFORE the first QA runs so even a crashed
    run is traceable. Includes commit SHA, timestamps, the exact CLI
    args, and a config snapshot."""
    from durin.config.loader import load_config

    cfg = load_config()
    try:
        preset = cfg.resolve_preset()
        model_resolved = preset.model
    except Exception:  # noqa: BLE001
        model_resolved = args.model

    manifest = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "commit": _git_commit_sha(),
        "args": vars(args),
        "subset_size": subset_size,
        "config_snapshot": {
            "model_resolved": model_resolved,
            "judge_model": args.judge_model,
            "data_path": args.data_path,
            "per_category": args.per_category,
            "seed": args.seed,
        },
        "durin_version": _durin_version(),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )


# Audit H15 (2026-05-29): the "iteration cap" marker was REMOVED
# from this list. Pre-H15 a trace whose got started with "I reached
# the maximum number of tool call iterations" was classified as
# infra — which queued it for a retry pass that always re-ran the
# same agent against the same workspace, hitting the same cap. The
# bench-100 v8 analysis confirmed: 4 of 5 iter-cap traces were
# agents falling back to grep/list_dir because memory_search didn't
# surface the answer. That's agent behaviour, not LLM provider
# instability — it deserves to be counted as a real fail so the
# next durin change has measurable signal to optimise against.
_INFRA_GOT_MARKERS = (
    "Error calling LLM",
    "Connection error",
)


def _is_infra_fail(trace: Any, verdict: dict[str, Any]) -> bool:
    """True when this fail is environmental (LLM provider error or
    timeout) rather than an agent/memory failure.

    These get retried once before being reported as fails — they
    inflate the error rate of durin without being durin's fault.

    Audit H15 (2026-05-29): iteration-cap fails are NO LONGER
    treated as infra. They reflect the agent burning its iteration
    budget without finding the answer — a real durin signal. Letta's
    public LoCoMo benchmark uses a similar policy: only LLM-provider
    transient errors are retried; iteration / budget exhaustion
    counts as a fail.
    """
    if verdict.get("score", 0) >= 1.0:
        return False
    got = (getattr(trace, "got", "") or "").strip()
    stop_reason = getattr(trace, "stop_reason", "") or ""
    if stop_reason in ("exception", "timeout"):
        return True
    return any(got.startswith(m) for m in _INFRA_GOT_MARKERS)


def _durin_version() -> str:
    try:
        import durin  # type: ignore
        return getattr(durin, "__version__", "?")
    except Exception:  # noqa: BLE001
        return "?"


async def _main_async(args: argparse.Namespace) -> int:
    from scripts.benchmark.locomo_analyze import analyze_run
    from scripts.benchmark.locomo_dataset import (
        LoCoMoDatasetError,
        load_dataset,
        stratified_subset,
    )
    from scripts.benchmark.locomo_harness import run_qa
    from scripts.benchmark.locomo_judge import JudgeError, judge_answer

    try:
        all_qas = load_dataset(args.data_path)
    except LoCoMoDatasetError as exc:
        print(f"[locomo_run] {exc}", file=sys.stderr)
        return 2

    if args.qa_id:
        subset = [q for q in all_qas if q.qa_id == args.qa_id]
        if not subset:
            print(f"[locomo_run] no QA matching {args.qa_id!r}", file=sys.stderr)
            return 2
    else:
        try:
            subset = stratified_subset(
                all_qas, per_category=args.per_category, seed=args.seed,
                allow_undersupplied=args.allow_undersupplied,
            )
        except LoCoMoDatasetError as exc:
            print(f"[locomo_run] {exc}", file=sys.stderr)
            return 2

    if args.resume_into:
        run_dir = Path(args.resume_into).expanduser()
        if not run_dir.is_dir():
            print(f"[locomo_run] --resume-into {run_dir} doesn't exist", file=sys.stderr)
            return 2
    else:
        run_dir = _new_run_dir(args.model, no_memory=args.no_memory)
        _write_manifest(run_dir, args=args, subset_size=len(subset))

    traces_dir = run_dir / "traces"
    telemetry_dir = run_dir / "telemetry"
    workspaces_dir = run_dir / "workspaces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    workspaces_dir.mkdir(parents=True, exist_ok=True)

    print(f"[locomo_run] run_dir: {run_dir.relative_to(_project_root())}")
    print(f"[locomo_run] {len(subset)} QAs to evaluate "
          f"(model={args.model}, judge={args.judge_model})")

    # Build a single shared LLM invoker. The harness uses durin's own
    # provider plumbing internally; the judge talks directly via
    # default_llm_invoke from durin.memory.dream (same z.ai plan).
    from durin.memory.dream import default_llm_invoke

    async def _run_one(qa, *, prefix: str) -> tuple[Any, dict[str, Any], bool]:
        """Run + judge a single QA. Returns (trace, verdict_dict, is_infra_fail)."""
        workspace = workspaces_dir / qa.qa_id
        telemetry_path = telemetry_dir / f"{qa.qa_id}.jsonl"
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
            verdict = judge_answer(
                qa.question, qa.answer, trace.got,
                llm_invoke=default_llm_invoke,
                model=args.judge_model,
            )
            verdict_dict.update({
                "score": verdict.score,
                "confidence": verdict.confidence,
                "reasoning": verdict.reasoning,
            })
        except JudgeError as exc:
            verdict_dict["reasoning"] = f"(judge failed) {exc}"
            verdict_dict["error"] = True

        is_infra = _is_infra_fail(trace, verdict_dict)

        trace_dict = trace.to_dict()
        trace_dict["verdict"] = verdict_dict
        (traces_dir / f"{qa.qa_id}.json").write_text(
            json.dumps(trace_dict, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if not args.keep_workspaces and workspace.exists():
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)
        return trace, verdict_dict, is_infra

    pass_count = 0
    fail_count = 0
    skip_count = 0
    infra_fail_qas: list = []
    for idx, qa in enumerate(subset, 1):
        trace_path = traces_dir / f"{qa.qa_id}.json"
        if args.resume and trace_path.exists():
            try:
                existing = json.loads(trace_path.read_text(encoding="utf-8"))
                if existing.get("verdict"):
                    skip_count += 1
                    print(f"  [{idx}/{len(subset)}] {qa.qa_id} [{qa.category}] — skip (already done)")
                    continue
            except Exception:  # noqa: BLE001
                pass  # malformed → re-run

        print(f"  [{idx}/{len(subset)}] {qa.qa_id} [{qa.category}] running…",
              end="", flush=True)

        trace, verdict_dict, is_infra = await _run_one(qa, prefix=f"[{idx}/{len(subset)}]")

        if verdict_dict["score"] >= 1.0:
            pass_count += 1
            print(f"  ✓ pass ({trace.duration_s:.1f}s, iter={trace.iterations})")
        else:
            fail_count += 1
            if is_infra:
                infra_fail_qas.append(qa)
            short_reason = verdict_dict["reasoning"][:80]
            tag = " [infra-retry-queued]" if is_infra else ""
            print(f"  ✗ fail ({trace.duration_s:.1f}s, iter={trace.iterations}) — {short_reason}{tag}")

    # B1+H18: re-run infrastructure fails (LLM provider transient errors,
    # exception, timeout). Two retry passes with a backoff between them so
    # a sustained upstream outage has a chance to recover. Pre-H18 the
    # bench used ONE retry pass; bench-100 v8 showed 9/15 infra-fails
    # didn't recover, partly because the upstream window hadn't closed by
    # the time the lone retry fired. The two-pass schedule (5s before
    # pass 1, 30s before pass 2) is a compromise: short enough that a
    # healthy provider sees no extra wall-clock, long enough to ride out
    # an outage burst.
    import asyncio as _asyncio_for_backoff
    if infra_fail_qas:
        recovered = 0
        retry_delays_s = (5.0, 30.0)  # H18: 2 passes with widening backoff
        remaining = list(infra_fail_qas)
        for pass_idx, delay_s in enumerate(retry_delays_s, start=1):
            if not remaining:
                break
            print(
                f"\n[locomo_run] retry pass {pass_idx}/{len(retry_delays_s)} "
                f"— {len(remaining)} QAs queued, sleeping {delay_s:.0f}s "
                f"to let upstream recover…"
            )
            await _asyncio_for_backoff.sleep(delay_s)
            next_remaining: list = []
            for qa in remaining:
                print(
                    f"  [retry-{pass_idx}] {qa.qa_id} [{qa.category}] running…",
                    end="", flush=True,
                )
                trace, verdict_dict, is_infra = await _run_one(
                    qa, prefix=f"[retry-{pass_idx}]",
                )
                if verdict_dict["score"] >= 1.0:
                    pass_count += 1
                    fail_count -= 1
                    recovered += 1
                    print(f"  ✓ recovered ({trace.duration_s:.1f}s)")
                elif is_infra:
                    # Still infra-fail: queue for next pass (if any).
                    next_remaining.append(qa)
                    short_reason = verdict_dict["reasoning"][:60]
                    print(
                        f"  ⟳ still infra (will retry again) — {short_reason}"
                    )
                else:
                    short_reason = verdict_dict["reasoning"][:60]
                    print(f"  ✗ still fail — {short_reason}")
            remaining = next_remaining
        if remaining:
            print(
                f"[locomo_run] {len(remaining)} QAs remain infra-failed "
                f"after {len(retry_delays_s)} retry passes — counted as fails"
            )
        print(f"[locomo_run] recovered via retry: {recovered}/{len(infra_fail_qas)}")

    print(f"\n[locomo_run] done: {pass_count} pass · {fail_count} fail · {skip_count} skip")
    print(f"[locomo_run] analyzing…")
    summary = analyze_run(run_dir)
    print(f"[locomo_run] score: {summary['score']:.3f} "
          f"({summary['n_pass']}/{summary['n_total']})")
    if summary.get("failure_breakdown"):
        print(f"[locomo_run] failure breakdown:")
        for cat, n in summary["failure_breakdown"].items():
            print(f"  - {cat}: {n}")
    print(f"[locomo_run] artifacts in: {run_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="locomo_run", description=__doc__)
    parser.add_argument(
        "--data-path", required=True,
        help="Path to the LoCoMo JSON dataset (download once from "
             "snap-research/locomo on GitHub).",
    )
    parser.add_argument(
        "--per-category", type=int, default=5,
        help="Stratified subset size per category (default 5 → 25 total).",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for stratified sampling (default 42 — change "
             "to evaluate variance, keep to compare across commits).",
    )
    parser.add_argument(
        "--model", default="glm-5-turbo",
        help="Agent model. Default glm-5-turbo via durin's z.ai provider.",
    )
    parser.add_argument(
        "--judge-model", default="glm-5-turbo",
        help="LLM-as-judge model. Same z.ai plan.",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=12,
        help=(
            "Cap on agent iterations per QA (default 12). Bumped from 8 "
            "in audit H16 (2026-05-29) after bench-100 v8 analysis "
            "showed iter-cap fails where the agent FOUND the answer at "
            "iteration 9-13 but ran out of budget — e.g. conv-3-q32 "
            "reached the correct ``Woodhaven`` URI on iteration 13. The "
            "default tracks the LoCoMo paper §4.2's `max_tool_calls=10` "
            "with margin for the multi-search + drill pattern memory-"
            "augmented agents use."
        ),
    )
    parser.add_argument(
        "--timeout-s", type=float, default=90.0,
        help="Hard per-QA wall-clock cap (default 90s).",
    )
    parser.add_argument(
        "--qa-id",
        help="Run only this single QA id (skip stratified sampling). Useful for debugging.",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="With --resume-into: skip QAs that already have a "
             "complete trace+verdict in the target run dir.",
    )
    parser.add_argument(
        "--resume-into",
        help="Write traces into an existing run dir instead of creating "
             "a new one. Useful after a crash.",
    )
    parser.add_argument(
        "--keep-workspaces", action="store_true",
        help="Don't delete the per-QA workspace after the run finishes "
             "(default: GC). Helpful for forensic inspection.",
    )
    parser.add_argument(
        "--no-memory", action="store_true",
        help="Ablation baseline: skip memory seeding. The agent answers "
             "cold (no conversation context injected). Run dir gets a "
             "_nomem suffix so results don't mix with memory-enabled runs.",
    )
    parser.add_argument(
        "--allow-undersupplied", action="store_true",
        help="Let stratified sampling take min(per_category, available) "
             "instead of failing. Needed for larger samples where some "
             "categories (e.g. adversarial in locomo10 has only 2) would "
             "otherwise cap the whole run.",
    )
    parser.add_argument(
        "--log-level", default="WARNING",
        help="Logging level for durin internals (default WARNING).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
