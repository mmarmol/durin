"""The default sub-workflow runner: run a named workflow as a nested run.

A subworkflow node delegates to this. It loads the named workflow and runs a nested
WorkflowEngine reusing the same node and branch-pick runners, so nodes inside a sub-workflow
behave exactly as at the top level. Cycles (a workflow that includes itself) are caught
precisely on re-entry by a call-stack guard. ``max_depth`` is the backstop for deeply
nested but non-cyclic workflows: beyond that limit it returns an error string instead of
recursing. A missing workflow returns an error string rather than raising, so one bad
reference does not abort the whole run.
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
        script_runner: Callable[[Any], Any] | None = None,
        max_depth: int = 5,
        _depth: int = 0,
        _stack: tuple[str, ...] = (),
    ) -> None:
        self.workspace = workspace
        self.node_runner = node_runner
        self.judge_runner = judge_runner
        self.script_runner = script_runner
        self.max_depth = max_depth
        self._depth = _depth
        self._stack = _stack

    def __call__(self, name: str, task: str, root_session_key: str | None = None,
                work_dir: str | None = None, parent_run_id: str | None = None,
                progress_emit: Any = None, cancel_check: Any = None,
                parent_node_id: str | None = None) -> str:
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
            script_runner=self.script_runner,
            max_depth=self.max_depth, _depth=self._depth + 1, _stack=self._stack + (name,),
        )

        # Tag nested frames with the node they run under: without it a surface
        # cannot distinguish a sub-workflow's nodes from the caller's own.
        def _tagged_emit(payload: dict) -> None:
            if progress_emit is None:
                return
            for frame in payload.get("nodes") or []:
                frame.setdefault("parent_node", parent_node_id)
            progress_emit(payload)

        engine = WorkflowEngine(
            node_runner=self.node_runner,
            script_runner=self.script_runner,
            subworkflow_runner=nested,
            workspace=str(self.workspace),
            pick_runner=self.judge_runner.pick if self.judge_runner is not None else None,
            progress_emit=(_tagged_emit if progress_emit is not None else None),
            cancel_check=cancel_check,
        )
        # Anchor the sub-workflow's node sessions to the invoking conversation too,
        # so nested work is navigable under it (no orphan subtrees).
        result = engine.run(workflow, task, root_session_key=root_session_key,
                            work_dir_override=work_dir, parent_run_id=parent_run_id)
        return result.final_output or ""
