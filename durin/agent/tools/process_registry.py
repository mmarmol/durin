"""Background process registry for exec(background=true).

Tracks long-lived shell processes (dev servers, builds, watchers) spawned by
the exec tool so the model can poll/kill them via the `process` tool instead
of blocking a turn. asyncio-native: one reader Task per process feeds a
rolling tail buffer; discovery is pure polling (pair with the `sleep` tool).

Adapted from hermes-agent's process_registry (MIT, Nous Research 2025),
minus the sync→async bridge (durin's loop is already async) and minus the
crash-recovery checkpoint (v1 limitation: a gateway restart orphans running
background processes — they keep running, untracked; AgentLoop shutdown
kills tracked process groups to bound this). See docs/internals/loop.md.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field

from loguru import logger

from durin.agent.tools._telemetry import emit_tool_event
from durin.config.schema import Base
from durin.utils.subprocess_cleanup import aclose_subprocess

_IS_WINDOWS = sys.platform == "win32"


class ProcessToolConfig(Base):
    """Background-process registry knobs (exec background=true / process tool)."""

    max_running: int = 16            # concurrent background processes
    max_output_chars: int = 200_000  # rolling tail buffer per process
    finished_ttl_s: int = 1800       # keep finished entries this long (30 min)


@dataclass
class ProcessSession:
    """One tracked background process."""

    id: str
    command: str
    cwd: str
    pid: int | None = None
    process: asyncio.subprocess.Process | None = None
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    exited: bool = False
    exit_code: int | None = None
    output_buffer: str = ""
    _reader_task: asyncio.Task | None = None

    @property
    def uptime_s(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at


class ProcessRegistry:
    """Process-global registry of background shell processes."""

    # Class attrs are the defaults; instance attrs (set in __init__) are what
    # the code reads, so config can override per-deployment.
    MAX_OUTPUT_CHARS = 200_000   # rolling tail per process
    MAX_RUNNING = 16             # concurrent background processes
    FINISHED_TTL_S = 1800.0      # finished entries pruned after 30 min
    MAX_FINISHED = 64

    def __init__(
        self,
        *,
        max_running: int | None = None,
        max_output_chars: int | None = None,
        finished_ttl_s: float | None = None,
    ) -> None:
        self.max_running = max_running if max_running is not None else self.MAX_RUNNING
        self.max_output_chars = (
            max_output_chars if max_output_chars is not None else self.MAX_OUTPUT_CHARS
        )
        self.finished_ttl_s = (
            finished_ttl_s if finished_ttl_s is not None else self.FINISHED_TTL_S
        )
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, ProcessSession] = {}

    # -- lifecycle ----------------------------------------------------------

    async def spawn(
        self, command: str, *, cwd: str, env: dict[str, str],
    ) -> ProcessSession:
        """Start *command* detached in its own process group and track it.

        Caller is responsible for command guarding/env curation (the exec
        tool runs its full guard pipeline before handing off).
        """
        self._prune_finished()
        if len(self._running) >= self.max_running:
            raise RuntimeError(
                f"Too many background processes ({len(self._running)} running, "
                f"max {self.max_running}). Kill one with the process tool first."
            )

        if _IS_WINDOWS:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=env,
            )
        else:
            bash = shutil.which("bash") or "/bin/bash"
            process = await asyncio.create_subprocess_exec(
                bash, "-l", "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=env,
                start_new_session=True,  # own process group → group kill works
            )

        sess = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            cwd=cwd,
            pid=process.pid,
            process=process,
        )
        sess._reader_task = asyncio.create_task(self._read_output(sess))
        self._running[sess.id] = sess
        emit_tool_event("process.spawn", {
            "proc_id": sess.id,
            "pid": sess.pid,
            "command_chars": len(command),
        })
        return sess

    async def _read_output(self, sess: ProcessSession) -> None:
        """Drain stdout into the rolling buffer; reap on EOF."""
        process = sess.process
        assert process is not None and process.stdout is not None
        try:
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                sess.output_buffer += chunk.decode("utf-8", errors="replace")
                if len(sess.output_buffer) > self.max_output_chars:
                    sess.output_buffer = sess.output_buffer[-self.max_output_chars:]
        except Exception as e:  # noqa: BLE001 — reader must never crash the loop
            logger.debug("process {} reader error: {}", sess.id, e)
        finally:
            with suppress(Exception):
                await process.wait()
            # Close the subprocess transport inside the running loop, else its
            # GC ``__del__`` runs after the loop is gone ("Event loop is
            # closed"). See durin/utils/subprocess_cleanup.py.
            await aclose_subprocess(process)
            sess.exited = True
            sess.exit_code = process.returncode
            self._move_to_finished(sess)
            emit_tool_event("process.exit", {
                "proc_id": sess.id,
                "pid": sess.pid,
                "exit_code": sess.exit_code,
                "runtime_s": round(sess.uptime_s, 3),
                "output_chars": len(sess.output_buffer),
            })

    def _move_to_finished(self, sess: ProcessSession) -> None:
        self._running.pop(sess.id, None)
        sess.finished_at = time.monotonic()
        self._finished[sess.id] = sess
        self._prune_finished()

    def _prune_finished(self) -> None:
        now = time.monotonic()
        stale = [
            sid for sid, s in self._finished.items()
            if now - (s.finished_at or now) > self.finished_ttl_s
        ]
        for sid in stale:
            del self._finished[sid]
        while len(self._finished) > self.MAX_FINISHED:
            oldest = min(
                self._finished.values(),
                key=lambda s: s.finished_at or 0.0,
            )
            del self._finished[oldest.id]

    # -- queries ------------------------------------------------------------

    def get(self, proc_id: str) -> ProcessSession | None:
        return self._running.get(proc_id) or self._finished.get(proc_id)

    def poll(self, proc_id: str, tail_chars: int = 2000) -> dict:
        sess = self.get(proc_id)
        if sess is None:
            return {"error": f"process '{proc_id}' not found"}
        return {
            "id": sess.id,
            "status": "exited" if sess.exited else "running",
            "pid": sess.pid,
            "exit_code": sess.exit_code,
            "uptime_s": round(sess.uptime_s, 1),
            "command": sess.command[:200],
            "output_tail": sess.output_buffer[-tail_chars:],
        }

    def list_sessions(self) -> list[dict]:
        entries = []
        for sess in list(self._running.values()) + list(self._finished.values()):
            entries.append({
                "id": sess.id,
                "status": "exited" if sess.exited else "running",
                "pid": sess.pid,
                "exit_code": sess.exit_code,
                "uptime_s": round(sess.uptime_s, 1),
                "command": sess.command[:120],
            })
        return entries

    # -- termination --------------------------------------------------------

    async def kill(self, proc_id: str, force: bool = False) -> dict:
        sess = self.get(proc_id)
        if sess is None:
            return {"killed": False, "error": f"process '{proc_id}' not found"}
        if sess.exited:
            return {"killed": False, "error": f"process '{proc_id}' already exited"}
        self._signal_group(sess, signal.SIGKILL if force else signal.SIGTERM)
        if not force:
            # Escalate to SIGKILL if the group ignores SIGTERM.
            for _ in range(50):
                if sess.exited:
                    break
                await asyncio.sleep(0.1)
            if not sess.exited:
                self._signal_group(sess, signal.SIGKILL)
        # Wait for the reader task to finish reaping (its finally does
        # ``await process.wait()``). This closes the subprocess transport
        # inside the running loop — otherwise its ``__del__`` runs after the
        # loop is gone and raises "Event loop is closed"
        # (PytestUnraisableExceptionWarning, and a noisy log in production).
        reader = sess._reader_task
        if reader is not None and not reader.done():
            with suppress(Exception):
                await asyncio.wait({reader}, timeout=5.0)
        emit_tool_event("process.kill", {
            "proc_id": sess.id,
            "pid": sess.pid,
            "force": force,
        })
        return {"killed": True, "id": sess.id}

    @staticmethod
    def _signal_group(sess: ProcessSession, sig: int) -> None:
        if sess.process is None or sess.pid is None:
            return
        if _IS_WINDOWS:
            with suppress(ProcessLookupError):
                sess.process.kill()
            return
        try:
            os.killpg(os.getpgid(sess.pid), sig)
        except (ProcessLookupError, PermissionError) as e:
            logger.debug("process {} signal {} failed: {}", sess.id, sig, e)

    async def shutdown(self) -> None:
        """Kill every running process group (agent shutdown hook)."""
        for sess in list(self._running.values()):
            with suppress(Exception):
                await self.kill(sess.id, force=True)


_registry: ProcessRegistry | None = None


def get_process_registry(
    config: ProcessToolConfig | None = None,
) -> ProcessRegistry:
    """Process-global singleton (durin is a single-process agent).

    ``config`` is applied only when the singleton is first created — the
    first tool to touch the registry (exec or process) configures it.
    """
    global _registry
    if _registry is None:
        if config is not None:
            _registry = ProcessRegistry(
                max_running=config.max_running,
                max_output_chars=config.max_output_chars,
                finished_ttl_s=config.finished_ttl_s,
            )
        else:
            _registry = ProcessRegistry()
    return _registry
