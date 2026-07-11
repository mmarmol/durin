"""The script node runner: run a deterministic subprocess for a ScriptNode.

Plugs into WorkflowEngine as the ``script_runner``, the peer of AgentNodeRunner.
The contract is Unix-plain: the upstream edge text arrives on stdin, small run
metadata in DURIN_* env vars, cwd is the run's shared working folder (the file
channel), stdout (capped) becomes the edge text to the next node. Routing is
derived deterministically — exit code for a binary gate (0 = PASS), the last
non-empty stdout line for a multi-way node — and returned as ``route_label`` so
the engine's routing path is identical to an agent node's. A non-zero exit on a
NON-gate node is an error (NodeExecutionError → aborted run), not a verdict.
A script node has no session: session_key is None and messages are empty.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from durin.workflow.engine import NodeExecutionError, NodeRunRequest, NodeRunResponse
from durin.workflow.verdict import parse_label

# Env values have platform size limits (unlike stdin); the task is a hint, not data.
_MAX_TASK_ENV_CHARS = 8000
_STDERR_TAIL_CHARS = 2000


class ScriptNodeRunner:
    def __init__(
        self,
        workspace: str | None,
        *,
        default_timeout: int = 300,
        max_output_chars: int = 16000,
    ) -> None:
        self._workspace = workspace
        self._default_timeout = default_timeout
        self._max_output_chars = max_output_chars

    def _argv(self, req: NodeRunRequest) -> list[str]:
        node = req.node
        if node.command:
            return ["bash", "-c", node.command]
        if self._workspace is None:
            raise NodeExecutionError(
                node.id, req.iteration, None,
                RuntimeError("a script-file node requires a workspace"))
        path = Path(self._workspace) / "workflows" / "scripts" / node.script
        if path.suffix == ".py":
            return [sys.executable, str(path)]
        if path.suffix == ".sh":
            return ["bash", str(path)]
        return [str(path)]   # anything else must be executable with a shebang

    def _cap(self, text: str) -> str:
        if len(text) <= self._max_output_chars:
            return text
        return (text[: self._max_output_chars].rstrip()
                + f"\n[output truncated at {self._max_output_chars} chars]")

    def __call__(self, req: NodeRunRequest) -> NodeRunResponse:
        node = req.node
        argv = self._argv(req)
        timeout = node.timeout or self._default_timeout
        cwd = req.output_dir
        if cwd:
            Path(cwd).mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update({
            "DURIN_TASK": (req.task or "")[:_MAX_TASK_ENV_CHARS],
            "DURIN_RUN_ID": req.run_id,
            "DURIN_NODE_ID": node.id,
            "DURIN_ITERATION": str(req.iteration),
        })
        if cwd:
            env["DURIN_WORK_DIR"] = cwd
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=cwd or None, env=env, text=True,
                start_new_session=True,   # own process group, so a timeout kill reaps children too
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise NodeExecutionError(node.id, req.iteration, None, exc) from exc
        try:
            stdout, stderr = proc.communicate(input=req.upstream_output or "", timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait()
            raise NodeExecutionError(
                node.id, req.iteration, None,
                TimeoutError(f"script timed out after {timeout}s")) from None
        rc = proc.returncode
        stderr_tail = (stderr or "").strip()[-_STDERR_TAIL_CHARS:]

        is_binary_gate = node.cases is None and (node.on_pass is not None or node.on_fail is not None)
        if is_binary_gate:
            if rc == 0:
                return NodeRunResponse(output=self._cap(stdout), session_key=None,
                                       route_label="PASS", exit_code=rc)
            # A failing gate's output is the loop-back feedback: what the check
            # printed, plus why it failed (stderr + exit code).
            parts = [p for p in (
                self._cap(stdout).strip(),
                f"[stderr]\n{stderr_tail}" if stderr_tail else "",
                f"[script gate failed: exit code {rc}]",
            ) if p]
            return NodeRunResponse(output="\n\n".join(parts), session_key=None,
                                   route_label="FAIL", exit_code=rc)

        if rc != 0:
            # Linear or multi-way node: a non-zero exit is an error, not a verdict —
            # continuing with half-produced output would poison the rest of the run.
            detail = f"script exited with code {rc}" + (f": {stderr_tail}" if stderr_tail else "")
            raise NodeExecutionError(node.id, req.iteration, None, RuntimeError(detail))

        label = parse_label(stdout, node.cases) if node.cases is not None else None
        return NodeRunResponse(output=self._cap(stdout), session_key=None,
                               route_label=label, exit_code=rc)
