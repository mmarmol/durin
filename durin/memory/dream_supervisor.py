"""Gateway-side supervision of dream worker subprocesses.

The gateway never runs dream passes in-process: it spawns
``durin memory dream-worker`` (its own Python re-exec) so the dream's LLM
calls, dulwich commits, and embedding batches burn a child's CPU/RAM — an
OOM kill or runaway pass takes down the dream, not the serving loop.

Everything here is deliberately synchronous and thread-friendly: the cron
path calls :func:`run_dream_worker` through ``asyncio.to_thread``; the
reactive triggers call it from their own daemon thread (they fire from
contexts where no event loop is guaranteed). Progress lines the worker
prints (JSONL, one payload per line) are forwarded to ``on_progress`` on
the calling thread; callers that need loop affinity wrap their callback
(see :func:`publish_threadsafe`).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from loguru import logger

_STDERR_TAIL_BYTES = 8192

# Live worker Popen objects, for shutdown termination. Guarded by _procs_lock;
# normally holds one entry (the dream lock in the worker rejects overlap).
_running_procs: set[subprocess.Popen] = set()
_procs_lock = threading.Lock()

# Event loop registered by the gateway's async entrypoint so non-loop threads
# (the reactive trigger thread) can hand progress payloads to the websocket
# bus safely. None outside a running gateway → progress is dropped, which is
# exactly the pre-subprocess behavior of the reactive path.
_progress_loop = None


def register_progress_loop(loop) -> None:
    global _progress_loop
    _progress_loop = loop


def publish_threadsafe(publish: Callable[[Any], None], payload: dict) -> None:
    """Run ``publish(payload)`` on the registered gateway loop, from any thread."""
    loop = _progress_loop
    if loop is None or loop.is_closed():
        logger.debug("dream progress dropped (no gateway loop registered)")
        return
    loop.call_soon_threadsafe(publish, payload)


def _worker_argv(mode: str, trigger: str) -> list[str]:
    return [
        sys.executable, "-m", "durin", "memory", "dream-worker",
        "--mode", mode, "--trigger", trigger,
    ]


def run_dream_worker(
    *,
    workspace: Path,
    mode: str,
    trigger: str,
    on_progress: Callable[[dict[str, Any]], None],
) -> tuple[int, str]:
    """Spawn one dream worker and pump it to completion (blocking).

    Returns ``(exit_code, stderr_tail)``. Protocol lines on the worker's
    stdout are JSON-decoded and handed to ``on_progress`` in order (on this
    thread); non-JSON lines are logged and skipped. If the worker exits
    without emitting a ``run_finished``, one is synthesized so UI running
    indicators always stop. After the worker exits — however it exits — the
    parent's alias-index cache for this workspace is invalidated, because
    the child's writes bypassed in-process invalidation.
    """
    argv = _worker_argv(mode, trigger)
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    with _procs_lock:
        _running_procs.add(proc)
    saw_finished = False
    stderr_tail = ""

    def _pump_stderr() -> None:
        nonlocal stderr_tail
        assert proc.stderr is not None
        while True:
            chunk = proc.stderr.read(4096)
            if not chunk:
                return
            stderr_tail = (stderr_tail + chunk)[-_STDERR_TAIL_BYTES:]

    err_thread = threading.Thread(
        target=_pump_stderr, daemon=True, name=f"dream-{trigger}-stderr"
    )
    err_thread.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("dream worker non-protocol stdout: {}", line[:200])
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("kind") == "run_finished":
                saw_finished = True
            try:
                on_progress(payload)
            except Exception:
                logger.exception("dream progress callback failed")
        code = proc.wait()
        err_thread.join(timeout=5)
    finally:
        with _procs_lock:
            _running_procs.discard(proc)
        # The worker wrote entity pages this process didn't see through its
        # own writers; drop the shared alias cache so the next lookup
        # rebuilds from disk.
        try:
            from durin.memory.aliases_cache import invalidate_alias_index

            invalidate_alias_index(Path(workspace) / "memory")
        except Exception:
            logger.exception("alias-cache invalidation after dream failed")
    if not saw_finished:
        try:
            on_progress({"kind": "run_finished", "ok": code == 0})
        except Exception:
            logger.exception("dream progress callback failed")
    return code, stderr_tail


def stop_dream_workers(grace_s: float = 10.0) -> None:
    """Terminate any running dream workers (gateway shutdown).

    SIGTERM, then SIGKILL after ``grace_s``. Killing between memory writes is
    safe — the store's flock + CAS discipline survives hard kills, and the
    per-session cursors resume the remainder on the next trigger.
    """
    with _procs_lock:
        procs = list(_running_procs)
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            continue
    deadline = grace_s
    for proc in procs:
        try:
            proc.wait(timeout=max(deadline, 0.1))
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
