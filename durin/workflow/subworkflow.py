"""The default sub-workflow runner: run a named workflow as a nested run.

A subworkflow node delegates to this. It loads the named workflow and runs a nested
WorkflowEngine reusing the same node and branch-pick runners, so nodes inside a sub-workflow
behave exactly as at the top level. It returns the child's full ``WorkflowResult`` so the
parent engine sees the child's terminal status — a child that pauses (needs_input), is
cancelled, or fails must not read as a completed node whose "output" is an error message.
Cycles (a workflow that includes itself) are caught precisely on re-entry by a call-stack
guard; ``max_depth`` is the backstop for deeply nested but non-cyclic workflows; a missing
workflow name is the same authoring-error class. All three return an aborted result rather
than raising, so the parent ends with a clear message instead of a traceback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from durin.workflow.engine import WorkflowEngine
from durin.workflow.loader import WorkflowNotFound, load_workflow
from durin.workflow.result import WorkflowResult


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
                parent_node_id: str | None = None) -> WorkflowResult:
        def _config_abort(message: str) -> WorkflowResult:
            # An unrunnable sub-workflow reference (cycle / depth / missing name) is an
            # authoring error: surface it as an aborted child result so the parent run
            # stops with the message, instead of threading it downstream as edge text.
            return WorkflowResult(status="aborted", final_output=message, runs=[], run_id="")

        if name in self._stack:
            chain = " -> ".join(self._stack + (name,))
            return _config_abort(f"Error: workflow cycle detected: {chain}")
        if self._depth >= self.max_depth:
            return _config_abort(f"Error: sub-workflow nesting exceeded max depth {self.max_depth}")
        try:
            workflow = load_workflow(self.workspace, name)
        except WorkflowNotFound as exc:
            return _config_abort(f"Error: {exc}")
        nested = SubworkflowRunner(
            self.workspace, self.node_runner, self.judge_runner,
            script_runner=self.script_runner,
            max_depth=self.max_depth, _depth=self._depth + 1, _stack=self._stack + (name,),
        )

        # Tag nested frames with the node they run under: without it a surface
        # cannot distinguish a sub-workflow's nodes from the caller's own.
        #
        # And re-key them onto the CALLER's run: surfaces key a work item by the
        # frame's run id, and the terminal frame is emitted for the caller's run
        # id only. A frame carrying the nested engine's own id therefore opens a
        # second work item that nothing ever closes — it sits "running" for the
        # rest of the session and inflates the active count. The nested engine
        # keeps its own run id for its own manifest; only the emitted payload is
        # re-keyed, and the payload is copied rather than mutated so the caller's
        # dict is left alone.
        def _tagged_emit(payload: dict) -> None:
            if progress_emit is None:
                return
            for frame in payload.get("nodes") or []:
                frame.setdefault("parent_node", parent_node_id)
            if parent_run_id:
                payload = {**payload, "run_id": parent_run_id}
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
        return engine.run(workflow, task, root_session_key=root_session_key,
                          work_dir_override=work_dir, parent_run_id=parent_run_id)
