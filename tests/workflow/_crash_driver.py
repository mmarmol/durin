"""The child process of the workflow crash tests: it runs one workflow so a test
can end this process while a chosen step is running.

    python -I tests/workflow/_crash_driver.py <workspace> <definition.json> <task>

Every step records each start as one line in ``<workspace>/marks/<step id>``.
The step whose id is in ``DURIN_TEST_HANG_IN`` writes ``<workspace>/marks/.hung``
and then sleeps, which is the moment the test ends the process. ``SIGUSR1``
makes the process replace its own image (same pid) with an idle interpreter,
which is what a restart in place looks like from outside.

This module imports nothing from the test suite. The harness imports it for
``build_engine`` so that a run continued inside the test process uses exactly
the runners the child used.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path

if __name__ == "__main__":
    # ``-I`` ignores PYTHONPATH and leaves the script's own directory off
    # sys.path, so put this checkout first: the child must run the same
    # ``durin`` the test process imported, not whatever else is installed.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from durin.workflow.engine import NodeRunResponse, WorkflowEngine  # noqa: E402
from durin.workflow.script_runner import ScriptNodeRunner  # noqa: E402
from durin.workflow.spec import parse_workflow  # noqa: E402
from durin.workflow.subworkflow import SubworkflowRunner  # noqa: E402

RUN_ID = "r1"
HANG_ENV = "DURIN_TEST_HANG_IN"
HANG_SECONDS = 120


def marks_dir(workspace: str | Path) -> Path:
    return Path(workspace) / "marks"


def _hangs(step_id: str) -> bool:
    return os.environ.get(HANG_ENV) == step_id


class StandInAgent:
    """Answers an agent step without a model: records the start, hangs when asked
    to, and otherwise replies ``<id>-out`` with a two-message conversation."""

    def __init__(self, workspace: str | Path) -> None:
        self._marks = marks_dir(workspace)

    def __call__(self, req):
        step = req.node.id
        self._marks.mkdir(parents=True, exist_ok=True)
        with open(self._marks / step, "a", encoding="utf-8") as fh:
            fh.write(step + "\n")
        if _hangs(step):
            (self._marks / ".hung").touch()
            time.sleep(HANG_SECONDS)
        output = f"{step}-out"
        if getattr(req.node, "tools", "none") == "default" and req.output_dir:
            Path(req.output_dir).mkdir(parents=True, exist_ok=True)
            Path(req.output_dir, f"{step}.txt").write_text(output + "\n", encoding="utf-8")
        return NodeRunResponse(
            output=output,
            session_key=f"workflow:{RUN_ID}:{step}:{req.iteration}",
            messages=[{"role": "user", "content": req.task},
                      {"role": "assistant", "content": output}],
        )


class MarkingScriptRunner:
    """The real script runner, with each inline command rewritten as it is
    dispatched: it first records the start, and the step that is to hang runs
    ``sleep`` instead of its own command. Rewriting at dispatch rather than in
    the definition keeps the definition (and its hash on the record) the one
    the test wrote, so the same definition can be resumed in the test process."""

    def __init__(self, workspace: str | Path) -> None:
        self._inner = ScriptNodeRunner(str(workspace))
        self._marks = marks_dir(workspace)

    def __call__(self, req):
        node = req.node
        if node.command:
            self._marks.mkdir(parents=True, exist_ok=True)
            mark = f"echo x >> {shlex.quote(str(self._marks / node.id))};"
            if _hangs(node.id):
                hung = shlex.quote(str(self._marks / ".hung"))
                command = f"{mark} touch {hung}; sleep {HANG_SECONDS}"
            else:
                command = f"{mark} {node.command}"
            req = dataclasses.replace(req, node=dataclasses.replace(node, command=command))
        return self._inner(req)


def build_engine(workspace: str | Path) -> WorkflowEngine:
    """The engine the child runs: stand-in agent, real script runner, run id
    ``r1``, and a real sub-workflow runner when ``<workspace>/workflows`` holds
    more than one definition."""
    workspace = Path(workspace)
    agent = StandInAgent(workspace)
    scripts = MarkingScriptRunner(workspace)
    definitions = list((workspace / "workflows").glob("*.json"))
    nested = (SubworkflowRunner(workspace, agent, script_runner=scripts)
              if len(definitions) > 1 else None)
    return WorkflowEngine(
        node_runner=agent,
        script_runner=scripts,
        run_id_factory=lambda: RUN_ID,
        workspace=str(workspace),
        subworkflow_runner=nested,
    )


def _replace_image(_signum, _frame) -> None:
    os.execv(sys.executable, [sys.executable, "-I", "-c", "import time; time.sleep(120)"])


def main(argv: list[str]) -> int:
    workspace, definition, task = Path(argv[1]), Path(argv[2]), argv[3]
    signal.signal(signal.SIGUSR1, _replace_image)
    workflow = parse_workflow(json.loads(definition.read_text(encoding="utf-8")))
    build_engine(workspace).run(workflow, task)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
