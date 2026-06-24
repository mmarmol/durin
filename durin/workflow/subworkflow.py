"""The default sub-workflow runner: run a named workflow as a nested run.

A subworkflow node delegates to this. It loads the named workflow and runs a nested
WorkflowEngine reusing the same node and branch-pick runners, so nodes inside a sub-workflow
behave exactly as at the top level. A depth counter caps nesting: beyond ``max_depth``
it returns an error string instead of recursing, which also bounds a cyclic reference
(a workflow that includes itself). A missing workflow returns an error string rather
than raising, so one bad reference does not abort the whole run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from durin.workflow.engine import WorkflowEngine
from durin.workflow.loader import WorkflowNotFound, load_workflow


class SubworkflowRunner:
    def __init__(
        self,
        workspace: str | Path,
        node_runner: Callable[[Any], Any],
        judge_runner: Callable[[str, str, "str | None"], Any] | None = None,
        *,
        max_depth: int = 5,
        _depth: int = 0,
        _stack: tuple[str, ...] = (),
    ) -> None:
        self.workspace = workspace
        self.node_runner = node_runner
        self.judge_runner = judge_runner
        self.max_depth = max_depth
        self._depth = _depth
        self._stack = _stack

    def __call__(self, name: str, task: str, root_session_key: str | None = None) -> str:
        if name in self._stack:
            chain = " -> ".join(self._stack + (name,))
            return f"Error: workflow cycle detected: {chain}"
        if self._depth >= self.max_depth:
            return f"Error: sub-workflow nesting exceeded max depth {self.max_depth}"
        try:
            workflow = load_workflow(self.workspace, name)
        except WorkflowNotFound as exc:
            return f"Error: {exc}"
        nested = SubworkflowRunner(
            self.workspace, self.node_runner, self.judge_runner,
            max_depth=self.max_depth, _depth=self._depth + 1, _stack=self._stack + (name,),
        )
        engine = WorkflowEngine(
            node_runner=self.node_runner,
            command_cwd=str(self.workspace),
            subworkflow_runner=nested,
            workspace=str(self.workspace),
            pick_runner=self.judge_runner.pick if self.judge_runner is not None else None,
        )
        # Anchor the sub-workflow's node sessions to the invoking conversation too,
        # so nested work is navigable under it (no orphan subtrees).
        result = engine.run(workflow, task, root_session_key=root_session_key)
        return result.final_output or ""
