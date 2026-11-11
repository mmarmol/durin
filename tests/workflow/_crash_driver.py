"""The child process of the workflow crash tests: it runs one workflow so a test
can end this process while a chosen step is running.

    python -I tests/workflow/_crash_driver.py <workspace> <definition.json> <task>

Every step records each start as one line in ``<workspace>/marks/<step id>``
(a script step first waits until the runner has recorded its process beside
the run's record, so a test that ends the process once the step has started
always finds the script recorded).
An agent step also records the attempt number it was given as one line in
``marks/<step id>.iter``, and what it was given (its task, the shared
conversation and the edge text) as one JSON line in ``marks/<step id>.seen``; a
script step records the stdin of its n-th start in ``marks/<step id>.stdin.<n>``.
The step whose id is in ``DURIN_TEST_HANG_IN`` (or the fan-out worker whose task
starts with ``DURIN_TEST_HANG_TASK``) writes ``<workspace>/marks/.hung`` and then
sleeps, which is the moment the test ends the process. When
``DURIN_TEST_HANG_AFTER`` names steps (comma-separated), it first waits until
each has started: ``<id>`` for a step, ``<id>=<prefix>`` for a start of agent step
``<id>`` whose task starts with ``<prefix>`` (a fan-out worker), so the test knows
what ran before the end. ``DURIN_TEST_OUTPUT_CHARS`` (``<id>=<n>``, comma-separated)
pads an agent step's reply to ``n`` characters. ``SIGUSR1`` makes the process replace its own image (same pid) with an
idle interpreter, which is what a restart in place looks like from outside.

Other switches: ``DURIN_TEST_RUN_ID`` gives the run another id than ``r1``;
``DURIN_TEST_WORK_KEY`` runs it on that work key; ``DURIN_TEST_RESUME=1``
resumes the dead run with that id instead of starting one, the way a resume
surface does (convert it, ask why it could not be resumed, enter it).

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
import threading
import time
from pathlib import Path

if __name__ == "__main__":
    # ``-I`` ignores PYTHONPATH and leaves the script's own directory off
    # sys.path, so put this checkout first: the child must run the same
    # ``durin`` the test process imported, not whatever else is installed.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from durin.workflow import run_log  # noqa: E402
from durin.workflow.engine import (  # noqa: E402
    NodeRunResponse,
    WorkflowEngine,
    build_resume_state,
    resume_refusal,
)
from durin.workflow.script_runner import ScriptNodeRunner  # noqa: E402
from durin.workflow.spec import parse_workflow  # noqa: E402
from durin.workflow.subworkflow import SubworkflowRunner  # noqa: E402

RUN_ID = "r1"
HANG_ENV = "DURIN_TEST_HANG_IN"
HANG_TASK_ENV = "DURIN_TEST_HANG_TASK"
HANG_AFTER_ENV = "DURIN_TEST_HANG_AFTER"
RUN_ID_ENV = "DURIN_TEST_RUN_ID"
WORK_KEY_ENV = "DURIN_TEST_WORK_KEY"
RESUME_ENV = "DURIN_TEST_RESUME"
OUTPUT_CHARS_ENV = "DURIN_TEST_OUTPUT_CHARS"
HANG_SECONDS = 120
_HANG_AFTER_WAIT_S = 20.0


def marks_dir(workspace: str | Path) -> Path:
    return Path(workspace) / "marks"


def sessions_dir(workspace: str | Path) -> Path:
    return Path(workspace) / "stand-in-sessions"


def _hangs(step_id: str, task: str | None = None) -> bool:
    if os.environ.get(HANG_ENV) == step_id:
        return True
    hang_task = os.environ.get(HANG_TASK_ENV)
    return bool(hang_task) and (task or "").strip().startswith(hang_task)


def seen(marks: Path, step: str) -> list[dict]:
    """What agent step ``step`` was given at each start, in start order."""
    path = marks / f"{step}.seen"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _started(marks: Path, entry: str) -> bool:
    step, _, prefix = entry.partition("=")
    if not prefix:
        return (marks / step).exists()
    return any((s.get("task") or "").strip().startswith(prefix) for s in seen(marks, step))


def _hang(marks: Path) -> None:
    """Wait until every start named in ``DURIN_TEST_HANG_AFTER`` happened, then
    flag the hang and sleep until the test ends this process."""
    after = [s for s in os.environ.get(HANG_AFTER_ENV, "").split(",") if s]
    deadline = time.monotonic() + _HANG_AFTER_WAIT_S
    while not all(_started(marks, s) for s in after) and time.monotonic() < deadline:
        time.sleep(0.02)
    (marks / ".hung").touch()
    time.sleep(HANG_SECONDS)


def _session_file(workspace: str | Path, key: str) -> Path:
    return sessions_dir(workspace) / (key.replace(":", "_").replace("/", "_") + ".json")


def load_shared_context(workspace: str | Path, key: str) -> list[dict]:
    """The conversation the stand-in agent saved under session ``key``."""
    return json.loads(_session_file(workspace, key).read_text(encoding="utf-8"))


class StandInAgent:
    """Answers an agent step without a model: records the start, hangs when asked
    to, and otherwise replies ``<id>-out`` with a two-message conversation, which
    it saves under its session key with the shared conversation it was given
    (what a real step's saved session holds)."""

    # Workers of one fan-out start on several threads: one start's lines are
    # written whole before the next start's.
    _write_lock = threading.Lock()

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace)
        self._marks = marks_dir(workspace)

    def __call__(self, req):
        step = req.node.id
        self._marks.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            with open(self._marks / step, "a", encoding="utf-8") as fh:
                fh.write(step + "\n")
            with open(self._marks / f"{step}.iter", "a", encoding="utf-8") as fh:
                fh.write(f"{req.iteration}\n")
            with open(self._marks / f"{step}.seen", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"task": req.task, "shared": req.shared_context,
                                     "upstream": req.upstream_output}) + "\n")
        if _hangs(step, req.task):
            _hang(self._marks)
        output = f"{step}-out"
        size = dict(e.split("=", 1) for e in os.environ.get(OUTPUT_CHARS_ENV, "").split(",")
                    if "=" in e).get(step)
        if size:
            output = output.ljust(int(size), "x")
        if getattr(req.node, "tools", "none") == "default" and req.output_dir:
            Path(req.output_dir).mkdir(parents=True, exist_ok=True)
            Path(req.output_dir, f"{step}.txt").write_text(output + "\n", encoding="utf-8")
        messages = [{"role": "user", "content": req.task},
                    {"role": "assistant", "content": output}]
        key = f"workflow:{req.run_id}:{step}:{req.iteration}"
        session = _session_file(self._workspace, key)
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(json.dumps(list(req.shared_context) + messages), encoding="utf-8")
        return NodeRunResponse(output=output, session_key=key, messages=messages)


class MarkingScriptRunner:
    """The real script runner, with each inline command rewritten as it is
    dispatched: it first records the start and keeps the stdin it was given, and
    the step that is to hang runs ``sleep`` instead of its own command. Rewriting at dispatch rather than in
    the definition keeps the definition (and its hash on the record) the one
    the test wrote, so the same definition can be resumed in the test process."""

    def __init__(self, workspace: str | Path) -> None:
        self._inner = ScriptNodeRunner(str(workspace))
        self._marks = marks_dir(workspace)

    def __call__(self, req):
        node = req.node
        if node.command:
            self._marks.mkdir(parents=True, exist_ok=True)
            marks = self._marks / node.id
            start = (len(marks.read_text(encoding="utf-8").splitlines())
                     if marks.is_file() else 0) + 1
            stdin = shlex.quote(str(self._marks / f"{node.id}.stdin.{start}"))
            mark = f"echo x >> {shlex.quote(str(marks))}; cat > {stdin};"
            if req.record_dir:
                # The runner records this process beside the run's record right
                # after starting it. Waiting for that record (bounded) before
                # marking the start means a test that ends the process once the
                # step has started always finds the script recorded.
                record = shlex.quote(str(Path(req.record_dir) / f"{req.run_id}.script.")) + "$$"
                mark = (f"for _ in $(seq 500); do [ -e {record} ] && break; sleep 0.01; done; "
                        + mark)
            if _hangs(node.id):
                hung = shlex.quote(str(self._marks / ".hung"))
                command = f"{mark} touch {hung}; sleep {HANG_SECONDS}"
            else:
                command = f"{mark} ( {node.command} ) < {stdin}"
            req = dataclasses.replace(req, node=dataclasses.replace(node, command=command))
        return self._inner(req)


def _pick_first(_criteria, _outputs, _model) -> int:
    """The stand-in judge of a ``choose`` step: the first branch wins."""
    return 0


def build_engine(workspace: str | Path, run_id: str = RUN_ID) -> WorkflowEngine:
    """The engine the child runs: stand-in agent, real script runner, a judge
    that picks the first branch, run id ``run_id``, a shared conversation read
    back from the stand-in's saved sessions, and a real sub-workflow runner when
    ``<workspace>/workflows`` holds more than one definition."""
    workspace = Path(workspace)
    agent = StandInAgent(workspace)
    scripts = MarkingScriptRunner(workspace)
    definitions = list((workspace / "workflows").glob("*.json"))
    nested = (SubworkflowRunner(workspace, agent, script_runner=scripts)
              if len(definitions) > 1 else None)
    return WorkflowEngine(
        node_runner=agent,
        script_runner=scripts,
        run_id_factory=lambda: run_id,
        workspace=str(workspace),
        subworkflow_runner=nested,
        pick_runner=_pick_first,
        shared_context_loader=lambda key: load_shared_context(workspace, key),
    )


def _replace_image(_signum, _frame) -> None:
    os.execv(sys.executable, [sys.executable, "-I", "-c", "import time; time.sleep(120)"])


def _resume(workspace: Path, workflow, run_id: str, task: str) -> int:
    """Resume the dead run ``run_id`` the way a resume surface does."""
    record = (run_log.mark_dead_run(workspace, workflow.name, run_id)
              or run_log.read_manifest(workspace, workflow.name, run_id))
    refusal = resume_refusal(record or {}, workspace=workspace, workflow=workflow)
    if record is None or refusal:
        print(f"run {run_id!r} cannot be resumed: {refusal}", file=sys.stderr)
        return 2
    build_engine(workspace, run_id).run(
        workflow, record.get("task") or task,
        resume=build_resume_state(record, "", workspace=workspace))
    return 0


def main(argv: list[str]) -> int:
    workspace, definition, task = Path(argv[1]), Path(argv[2]), argv[3]
    signal.signal(signal.SIGUSR1, _replace_image)
    workflow = parse_workflow(json.loads(definition.read_text(encoding="utf-8")))
    run_id = os.environ.get(RUN_ID_ENV) or RUN_ID
    if os.environ.get(RESUME_ENV) == "1":
        return _resume(workspace, workflow, run_id, task)
    build_engine(workspace, run_id).run(
        workflow, task, work_key=os.environ.get(WORK_KEY_ENV) or None)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
