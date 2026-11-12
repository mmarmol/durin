"""The script node runner: run a deterministic subprocess for a ScriptNode.

Plugs into WorkflowEngine as the ``script_runner``, the peer of AgentNodeRunner.
The contract is Unix-plain: stdin carries the upstream edge text. A start-position
node (no upstream) receives the run's task instead, since the run's input text is
the incoming edge of the start node. That task is exactly what the caller passed:
the workflow's input/output descriptions frame the task for agent nodes only, never
for a script, which parses its input as data. Upstream producers that emit an empty string
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

While the subprocess runs it is recorded beside the run's record
(``run_log.record_script``), and the record is removed once it has exited. A
record left by a process that died tells a resume of the run that the script
may still run: the resume is refused until nothing of it is left.

This process keeps the scripts it started and has not reaped, and ends them
all only at the point it ends or replaces itself (``end_scripts_for_exit``,
called right before ``os._exit`` or ``os.execv``). Apart from that, a script's
group is signalled only by the thread that started it, before that thread
reaps it, on a timeout or a cancel.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import NoReturn

from loguru import logger

from durin.security.secrets import get_secret_store, redact_secrets, scope_allows
from durin.workflow import run_log
from durin.workflow.engine import (
    NodeExecutionError,
    NodeRunRequest,
    NodeRunResponse,
    ScriptCancelledError,
)
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

# The scripts this process started and has not reaped, and whether this process
# is ending. One lock covers both, and it is held only to read the one and
# change the other: a script starts outside it, so neither the end-of-process
# pass nor another run's script ever waits for a start. A script is either in
# the set when the pass runs (and ended by it), or its starting thread sees,
# under the lock, that the pass has run and ends it itself; a thread whose
# script was reaped learns under it whether the pass has run.
_children_lock = threading.Lock()
_children: set[subprocess.Popen] = set()
_exiting = False

# How long the end-of-process pass waits to hold a script's reap lock. The
# thread that started the script takes it only for an instant per poll, except
# right after it has killed the script's group itself, while it reaps it; a
# script whose lock stays busy past this is that one, already ended.
_REAP_LOCK_WAIT_S = 1.0


def _wait_for_exit() -> NoReturn:
    """Hold this thread until the process ends: it has nothing left to do,
    since anything it did now could record the outcome of a script the
    process's own exit ended."""
    threading.Event().wait()
    raise AssertionError("unreachable: the wait on an event nobody sets never returns")


def _signal_unreaped(proc: subprocess.Popen) -> bool:
    """Send SIGKILL to the group ``proc`` leads while ``proc`` is not reaped;
    return whether it was sent. ``Popen`` reaps a child, and sets its
    ``returncode``, only while holding its own reap lock (``_waitpid_lock``):
    holding it here means the child cannot be reaped between the check and
    the signal, so its pid, which is the group's id, is still this script's."""
    reap_lock = proc._waitpid_lock
    if not reap_lock.acquire(timeout=_REAP_LOCK_WAIT_S):
        return False
    try:
        if proc.returncode is not None:
            return False
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            return False
        return True
    finally:
        reap_lock.release()


def end_scripts_for_exit() -> int:
    """End every script this process started and has not reaped, and mark the
    process as ending; returns how many groups were signalled. Called only
    where the next statement ends or replaces the process (``os._exit``,
    ``os.execv``).

    From then on no script is started, and a thread whose script has been
    reaped waits until the process ends instead of returning, raising or
    removing the script's record: a script this pass ended (exit code -9) is
    never a failed step, a gate's ``FAIL`` or a route taken, and its record
    stays for whoever checks the run next. It reaps nothing: a script is
    reaped only by the thread that started it, so that thread's own kills (on
    a timeout or a cancel) always reach a pid that is still its script's.
    Never raises."""
    global _exiting
    ended = 0
    with _children_lock:
        _exiting = True
        for proc in _children:
            try:
                if _signal_unreaped(proc):
                    ended += 1
            except Exception:  # noqa: BLE001 - the process must still end
                continue
    return ended


def _start(argv: list[str], cwd: str | None, env: dict[str, str]) -> subprocess.Popen:
    """Start a script and keep it among this process's children; wait for the
    process to end instead when the process is ending.

    The start runs outside the lock: a start can block (a working folder on a
    stalled mount), and the pass and every other script must not wait for it.
    When the pass ran while this script was starting, the script is not among
    the children it signalled: this thread ends it (its own child, not yet
    reaped), records nothing and waits for the process to end. If the process
    ends before this thread gets there, the script outlives it unrecorded."""
    with _children_lock:
        exiting = _exiting
    if exiting:
        _wait_for_exit()
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=cwd, env=env, text=True,
        errors="replace",   # a script emitting non-UTF-8 bytes must degrade, not crash the runner
        start_new_session=True,   # own process group, so a timeout kill reaps children too
    )
    with _children_lock:
        exiting = _exiting
        if not exiting:
            _children.add(proc)
    if exiting:
        ScriptNodeRunner._end(proc)
        _wait_for_exit()
    return proc


def _release(proc: subprocess.Popen) -> None:
    """Forget a reaped script; wait for the process to end instead when the
    process is ending, so nothing of the script's outcome leaves the runner."""
    with _children_lock:
        _children.discard(proc)
        exiting = _exiting
    if exiting:
        _wait_for_exit()


class ScriptNodeRunner:
    def __init__(
        self,
        workspace: str | None,
        *,
        default_timeout: int = 300,
        max_output_chars: int = 16000,
        log_max_chars: int = 4000,
    ) -> None:
        self._workspace = workspace
        self._default_timeout = default_timeout
        self._max_output_chars = max_output_chars
        # Separate from _max_output_chars: that one bounds the edge text handed to
        # the next node, this one bounds what the manifest stores. The manifest is
        # rewritten in full after every node, so what it keeps stays tighter.
        self._log_max_chars = log_max_chars

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

    def _cap_log(self, text: str) -> str:
        if len(text) <= self._log_max_chars:
            return text
        return (text[: self._log_max_chars].rstrip()
                + f"\n[truncated at {self._log_max_chars} chars]")

    @staticmethod
    def _display_command(node) -> str:
        """What this node ran, as a reader would recognize it: the inline command,
        or the script's workspace-relative path. Not the resolved argv — the
        absolute interpreter path is noise, and the point is recognition.

        Redacted like the streams are: this text is written to the manifest, and a
        command line is as capable of carrying a credential as the output is.
        """
        return redact_secrets(node.command or f"workflows/scripts/{node.script}")

    def _base_env(self, node) -> dict[str, str]:
        if node.env == "inherit":
            # The gateway's environment also holds credentials it loaded from
            # the secret store for its own clients (provider API keys). A
            # stored secret reaches a script only when declared, so a
            # variable carrying a stored value is left out of the ambient rest.
            stored = {
                entry.value for entry in get_secret_store().all().values()
                if isinstance(entry.value, str) and entry.value
            }
            return {k: v for k, v in os.environ.items() if v not in stored}
        return {k: os.environ[k] for k in _CLEAN_ENV_ALLOWLIST if k in os.environ}

    def _declared_secrets(self, req: NodeRunRequest) -> dict[str, str]:
        """Resolve the node's declared secret names for env injection. Neither env
        mode carries stored secrets (``inherit`` leaves out any variable holding a
        stored value), so a script authenticates only via this explicit manifest. A
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
                    RuntimeError(f"declared secret {name!r} is not in the secret store "
                                 f"(the `workflows` skill documents script-node secrets)"))
            if not scope_allows(entry.scope, "exec"):
                raise NodeExecutionError(
                    node.id, req.iteration, None,
                    RuntimeError(f"secret {name!r} does not allow the 'exec' scope — "
                                 f"grant it before this node can use it (see the "
                                 f"`workflows` skill, script-node reference)"))
            out[name] = entry.value
        return out

    @staticmethod
    def _killpg(proc: subprocess.Popen) -> tuple[str, str]:
        """Kill the process group and return whatever it printed before dying.

        Returns the drained streams rather than discarding them: a script killed
        by a timeout or a cancel has usually printed the very lines that explain
        where it got stuck.
        """
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        # Drain and close the pipes rather than a bare wait() — per the subprocess
        # docs, communicate() after a kill is what reaps the process AND closes
        # stdout/stderr, avoiding an fd leak.
        out, err = proc.communicate()
        return out or "", err or ""

    def __call__(self, req: NodeRunRequest) -> NodeRunResponse:
        node = req.node
        argv = self._argv(req)
        timeout = node.timeout or self._default_timeout
        cwd = req.output_dir
        if cwd:
            Path(cwd).mkdir(parents=True, exist_ok=True)
        env = self._base_env(node)
        env.update(self._declared_secrets(req))
        task = req.raw_task if req.raw_task is not None else req.task
        # DURIN_* metadata is set last so a declared secret can never shadow it.
        env.update({
            "DURIN_TASK": (task or "")[:_MAX_TASK_ENV_CHARS],
            "DURIN_RUN_ID": req.run_id,
            "DURIN_NODE_ID": node.id,
            "DURIN_ITERATION": str(req.iteration),
        })
        if cwd:
            env["DURIN_WORK_DIR"] = cwd
        try:
            proc = _start(argv, cwd or None, env)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise NodeExecutionError(node.id, req.iteration, None, exc) from exc
        record = None
        try:
            record = self._record_child(req, proc)
            return self._finish(req, proc, task, timeout)
        finally:
            if proc.returncode is None:
                # Something other than the script's own end or a kill below
                # broke the wait: end the script here, so it never outlives
                # its step unrecorded.
                self._end(proc)
            _release(proc)
            run_log.forget_script(record)

    @staticmethod
    def _record_child(req: NodeRunRequest, proc: subprocess.Popen) -> Path | None:
        """Record the script process beside the run's record while it runs, so
        that if this process dies, a resume of the run waits until nothing of
        the script is left before its step runs again. Nothing is recorded when
        the engine keeps no record, or when the start time cannot be read (the
        process could not be told from a later one with the same pid). A failed
        write does not fail the step."""
        if not req.record_dir:
            return None
        from durin.utils.process_tree import process_identity

        started = process_identity(proc.pid)["started"]
        if started is None:
            if proc.poll() is not None:
                return None     # it already exited: nothing left to record
            logger.warning("script of step {!r} in run {}: its start could not be read, "
                           "so its process (pid {}) is not recorded; if this process dies "
                           "while it runs, a resume of the run cannot wait for it",
                           req.node.id, req.run_id, proc.pid)
            return None
        try:
            return run_log.record_script(req.record_dir, req.run_id, pid=proc.pid,
                                         started=started, node_id=req.node.id)
        except OSError:
            logger.warning("script of step {!r} in run {}: could not record its process",
                           req.node.id, req.run_id)
            return None

    @staticmethod
    def _end(proc: subprocess.Popen) -> None:
        """Kill the script's process group and reap it, without reading its
        streams (whatever broke the wait may break a read too)."""
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with suppress(Exception):
            proc.kill()
        with suppress(Exception):
            proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with suppress(Exception):
                    stream.close()

    def _finish(self, req: NodeRunRequest, proc: subprocess.Popen, task: str | None,
                timeout: int) -> NodeRunResponse:
        """Feed the script its input, wait for it (killing it on a timeout or a
        cancel) and turn what it printed into the step's response."""
        node = req.node
        stdin_data = (req.upstream_output if req.upstream_output is not None else task) or ""
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
                    killed_out, killed_err = self._killpg(proc)
                    raise NodeExecutionError(
                        node.id, req.iteration, None,
                        TimeoutError(f"script timed out after {timeout}s"),
                        command=self._display_command(node),
                        stdout=self._cap_log(redact_secrets(killed_out)),
                        stderr=self._cap_log(redact_secrets(killed_err)),
                    ) from None
                if req.cancel_check is not None and req.cancel_check():
                    killed_out, killed_err = self._killpg(proc)
                    raise NodeExecutionError(
                        node.id, req.iteration, None,
                        ScriptCancelledError("cancelled by user"),
                        command=self._display_command(node),
                        stdout=self._cap_log(redact_secrets(killed_out)),
                        stderr=self._cap_log(redact_secrets(killed_err)),
                    ) from None
                try:
                    stdout, stderr = proc.communicate(timeout=min(_POLL_SLICE_SECONDS, remaining))
                except subprocess.TimeoutExpired:
                    continue
        rc = proc.returncode
        # Redact stored secret values out of both streams before any edge, feedback
        # or manifest text is built — all three persist, so a script echoing a
        # credential must never leave it behind in any of them.
        stdout = redact_secrets(stdout or "")
        stderr = redact_secrets(stderr or "")
        stderr_tail = stderr.strip()[-_STDERR_TAIL_CHARS:]
        # What the manifest records: the same redacted streams, capped tighter
        # than the edge text, plus the command as a reader would recognize it.
        logs = {
            "command": self._display_command(node),
            "stdout": self._cap_log(stdout),
            "stderr": self._cap_log(stderr),
        }

        # Whether the edge text below is a cut copy of what the script printed. A
        # consumer that parses the output as data (a fan-out reading its list) must
        # know this from the response, not by recognising the notice in the text.
        truncated = len(stdout) > self._max_output_chars

        is_binary_gate = node.cases is None and (node.on_pass is not None or node.on_fail is not None)
        if is_binary_gate:
            if rc == 0:
                return NodeRunResponse(output=self._cap(stdout), session_key=None,
                                       route_label="PASS", exit_code=rc,
                                       output_truncated=truncated, **logs)
            # A non-zero exit's output is what the check printed plus its stderr
            # tail and exit code. Into a step that leads back to this gate on a
            # route of its own (req.fail_loops_back), it is loop-back feedback and
            # says the gate failed; otherwise it is the check's normal "no", so the
            # note stays a plain exit code instead of reporting a failure that never
            # happened.
            exit_note = (f"[script gate failed: exit code {rc}]" if req.fail_loops_back
                         else f"[exit code {rc}]")
            parts = [p for p in (
                self._cap(stdout).strip(),
                f"[stderr]\n{stderr_tail}" if stderr_tail else "",
                exit_note,
            ) if p]
            return NodeRunResponse(output="\n\n".join(parts), session_key=None,
                                   route_label="FAIL", exit_code=rc,
                                   output_truncated=truncated, **logs)

        if rc != 0:
            # Linear or multi-way node: a non-zero exit is an error, not a verdict —
            # continuing with half-produced output would poison the rest of the run.
            detail = f"script exited with code {rc}" + (f": {stderr_tail}" if stderr_tail else "")
            raise NodeExecutionError(node.id, req.iteration, None, RuntimeError(detail),
                                     exit_code=rc, **logs)

        label = parse_label(stdout, node.cases) if node.cases is not None else None
        return NodeRunResponse(output=self._cap(stdout), session_key=None,
                               route_label=label, exit_code=rc,
                               output_truncated=truncated, **logs)
