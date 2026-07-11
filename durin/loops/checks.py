"""Goal verification: script checks are hard evidence, the judge only gets to
be stricter. A failing required check blocks 'done' no matter what the model
thinks."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from durin.loops.spec import LoopSpec

AssertJudge = Callable[[str, list[str], str], Awaitable[dict]]


@dataclass
class GoalVerdict:
    reached: bool
    results: list[dict] = field(default_factory=list)
    intent_met: bool | None = None


def _run_script(command: str, work_dir: str | None, timeout_s: int) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=work_dir, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout_s}s"
    detail = (proc.stdout or proc.stderr or "").strip()[:500]
    return proc.returncode == 0, detail or f"exit {proc.returncode}"


async def verify_goal(spec: LoopSpec, evidence: str, *, judge: AssertJudge,
                      work_dir: str | None, timeout_s: int) -> GoalVerdict:
    results: list[dict] = []
    required_ok = True

    for c in spec.checks:
        if c.kind != "script":
            continue
        passed, detail = await asyncio.to_thread(_run_script, c.command, work_dir, timeout_s)
        results.append({"kind": "script", "required": c.required, "ref": c.command, "passed": passed, "detail": detail})
        if c.required and not passed:
            required_ok = False

    if spec.checks_sufficient:
        # Validated at parse time: checks_sufficient=True means every check is
        # a script, so the required-check results above are the whole verdict
        # — no judge call, no intent_met (the intent is never graded here).
        return GoalVerdict(reached=required_ok, results=results, intent_met=None)

    assertion_texts = [c.text for c in spec.checks if c.kind == "assertion"]
    verdict = await judge(spec.goal_intent, assertion_texts, evidence)
    intent_met = bool(verdict.get("intent_met"))
    judged = verdict.get("assertions") or {}
    for c in spec.checks:
        if c.kind != "assertion":
            continue
        passed = bool(judged.get(c.text, False))
        results.append({"kind": "assertion", "required": c.required, "ref": c.text, "passed": passed, "detail": ""})
        if c.required and not passed:
            required_ok = False

    return GoalVerdict(reached=required_ok and intent_met, results=results, intent_met=intent_met)
