"""The script node runner: run a deterministic subprocess for a ScriptNode.

Plugs into WorkflowEngine as the ``script_runner``, the peer of AgentNodeRunner.
The contract is Unix-plain: stdin carries the upstream edge text. A start-position
node (no upstream) receives the run's task instead, since the run's input text is
the incoming edge of the start node. Upstream producers that emit an empty string
produce empty stdin (no fallback). Small run metadata in DURIN_* env vars, cwd is
the run's shared working folder (the file channel), stdout (capped) becomes the
edge text to the next node. The subprocess environment is a minimal allowlist plus
DURIN_* by default (the node's ``env: "clean"``); a node opting into
``env: "inherit"`` gets the full gateway process environment instead. Neither
mode carries stored secrets: a node names the ones it needs in ``secrets`` and
they are resolved from the secret store (each must allow the ``exec`` scope)
into the subprocess env, with stdout/stderr redacted against the store before
becoming edge text. Routing is
derived deterministically — exit code for a binary gate (0 = PASS), the last
non-empty stdout line for a multi-way node —
and returned as ``route_label`` so the engine's routing path is identical to an
agent node's. A non-zero exit on a NON-gate node is an error (NodeExecutionError
→ aborted run), not a verdict. A script node has no session: session_key is None
and messages are empty.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from durin.security.secrets import get_secret_store, redact_secrets, scope_allows
from durin.workflow.engine import NodeExecutionError, NodeRunRequest, NodeRunResponse, ScriptCancelled
from durin.workflow.verdict import parse_label

# Env values have platform size limits (unlike stdin); the task is a hint, not data.
_MAX_TASK_ENV_CHARS = 8000
_STDERR_TAIL_CHARS = 2000
# How often the run loop wakes up to check the overall deadline and the cooperative
# cancel flag while a script is still running. Small enough that a cancel takes
# effect promptly; large enough not to busy-poll.
_POLL_SLICE_SECONDS = 0.5

# "clean" env (the node default): just enough of the gateway environment for a
# script to behave normally (PATH-resolved binaries, locale, a writable tmp dir)
# without forwarding ambient provider keys or other secrets the gateway process holds.
# DURIN_HOME passes through so a script invoking the `durin` CLI targets the same
# home the running gateway uses (it is durin's own pointer, not a secret).
_CLEAN_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
    "DURIN_HOME",
)


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

    def _base_env(self, node) -> dict[str, str]:
        if node.env == "inherit":
            return dict(os.environ)
        return {k: os.environ[k] for k in _CLEAN_ENV_ALLOWLIST if k in os.environ}

    def _declared_secrets(self, req: NodeRunRequest) -> dict[str, str]:
        """Resolve the node's declared secret names for env injection. Neither env
        mode carries stored secrets (they live in the secret store, not the gateway
        environment), so a script authenticates only via this explicit manifest. A
        name absent from the store, or whose scope does not authorize the ``exec``
        consumer, is the author's error: fail the node naming it instead of running
        the script with a silently missing credential."""
        node = req.node
        if not getattr(node, "secrets", ()):
            return {}
        entries = get_secret_store().all()
        out: dict[str, str] = {}
        for name in node.secrets:
            entry = entries.get(name)
            if entry is None:
                raise NodeExecutionError(
                    node.id, req.iteration, None,
                    RuntimeError(f"declared secret {name!r} is not in the secret store"))
            if not scope_allows(entry.scope, "exec"):
                raise NodeExecutionError(
                    node.id, req.iteration, None,
                    RuntimeError(f"secret {name!r} does not allow the 'exec' scope — "
                                 f"grant it before this node can use it"))
            out[name] = entry.value
        return out

    @staticmethod
    def _killpg(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        # Drain and close the pipes rather than a bare wait() — per the subprocess
        # docs, communicate() after a kill is what reaps the process AND closes
        # stdout/stderr, avoiding an fd leak.
        proc.communicate()

    def __call__(self, req: NodeRunRequest) -> NodeRunResponse:
        node = req.node
        argv = self._argv(req)
        timeout = node.timeout or self._default_timeout
        cwd = req.output_dir
        if cwd:
            Path(cwd).mkdir(parents=True, exist_ok=True)
        env = self._base_env(node)
        env.update(self._declared_secrets(req))
        # DURIN_* metadata is set last so a declared secret can never shadow it.
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
                errors="replace",   # a script emitting non-UTF-8 bytes must degrade, not crash the runner
                start_new_session=True,   # own process group, so a timeout kill reaps children too
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise NodeExecutionError(node.id, req.iteration, None, exc) from exc
        stdin_data = (req.upstream_output if req.upstream_output is not None else req.task) or ""
        deadline = time.monotonic() + timeout
        try:
            stdout, stderr = proc.communicate(input=stdin_data, timeout=min(_POLL_SLICE_SECONDS, timeout))
        except subprocess.TimeoutExpired:
            # Retry communicate() after a TimeoutExpired — the documented-safe
            # pattern (the input has already been sent, so later calls take no
            # `input`). Each slice re-checks the overall deadline (unchanged
            # timeout semantics) and, only for a script node, the cooperative
            # cancel flag — letting a cancel kill a running subprocess instead of
            # only taking effect between nodes.
            stdout = stderr = None
            while stdout is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._killpg(proc)
                    raise NodeExecutionError(
                        node.id, req.iteration, None,
                        TimeoutError(f"script timed out after {timeout}s")) from None
                if req.cancel_check is not None and req.cancel_check():
                    self._killpg(proc)
                    raise NodeExecutionError(
                        node.id, req.iteration, None,
                        ScriptCancelled("cancelled by user")) from None
                try:
                    stdout, stderr = proc.communicate(timeout=min(_POLL_SLICE_SECONDS, remaining))
                except subprocess.TimeoutExpired:
                    continue
        rc = proc.returncode
        # Redact stored secret values out of both streams before any edge/feedback
        # text is built — stdout becomes edge text that lands in sessions, manifests
        # and memory, so a script echoing a credential must never persist it.
        stdout = redact_secrets(stdout or "")
        stderr_tail = redact_secrets((stderr or "").strip()[-_STDERR_TAIL_CHARS:])

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
            raise NodeExecutionError(node.id, req.iteration, None, RuntimeError(detail), exit_code=rc)

        label = parse_label(stdout, node.cases) if node.cases is not None else None
        return NodeRunResponse(output=self._cap(stdout), session_key=None,
                               route_label=label, exit_code=rc)
