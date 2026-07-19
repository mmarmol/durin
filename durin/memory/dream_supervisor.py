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
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from loguru import logger

_STDERR_TAIL_BYTES = 8192

# RSS watchdog sampling period. Coarse on purpose: the observed runaway grew
# over minutes, and each sample is one `ps` snapshot.
_WATCHDOG_INTERVAL_S = 5.0

# With no explicit cap, the worker tree may use this fraction of total RAM
# before the watchdog terminates it.
_AUTO_RSS_FRACTION = 0.4

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


def _worker_argv(mode: str, trigger: str, workspace: Path) -> list[str]:
    return [
        sys.executable, "-m", "durin", "memory", "dream-worker",
        "--mode", mode, "--trigger", trigger,
        "--workspace", str(workspace),
    ]


def _terminate_worker_tree(proc: subprocess.Popen, *, grace_s: float = 10.0) -> None:
    """SIGTERM the worker's whole process group (it owns one via
    ``start_new_session``), escalate to SIGKILL after *grace_s*. Killing
    only the worker would orphan its embedding pool children."""
    def _signal_group(sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (OSError, AttributeError):
            try:
                proc.send_signal(sig)
            except OSError:
                pass

    _signal_group(signal.SIGTERM)
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        _signal_group(signal.SIGKILL)


def reactive_memory_gate_ok(min_available_mb: int) -> tuple[bool, float]:
    """(may_spawn, available_mb) for the reactive-dream memory gate.

    A reactive dream is a freshness optimization — launching one into an
    already-tight host is how the 2026-07-18 incident started. Unknown
    availability (0.0) means "no signal", never "no memory": the gate stays
    open, because a false skip on platforms without the metric would silently
    starve reactive dreaming. ``min_available_mb <= 0`` disables the gate.
    """
    if min_available_mb <= 0:
        return True, 0.0
    from durin.utils.process_tree import available_memory_mb

    available = available_memory_mb()
    if available <= 0.0:
        return True, available
    return available >= min_available_mb, available


def _resolve_rss_cap_mb(max_rss_mb: int | None) -> float:
    """The effective watchdog cap: an explicit positive value wins; otherwise
    a fraction of total RAM; 0.0 (watchdog off) when RAM size is unknown."""
    if max_rss_mb and max_rss_mb > 0:
        return float(max_rss_mb)
    from durin.utils.process_tree import total_memory_mb

    total = total_memory_mb()
    return round(total * _AUTO_RSS_FRACTION, 1) if total else 0.0


def run_dream_worker(
    *,
    workspace: Path,
    mode: str,
    trigger: str,
    on_progress: Callable[[dict[str, Any]], None],
    max_rss_mb: int | None = None,
) -> tuple[int, str]:
    """Spawn one dream worker and pump it to completion (blocking).

    Returns ``(exit_code, stderr_tail)``. Protocol lines on the worker's
    stdout are JSON-decoded and handed to ``on_progress`` in order (on this
    thread); non-JSON lines are logged and skipped. If the worker exits
    without emitting a ``run_finished``, one is synthesized so UI running
    indicators always stop. After the worker exits — however it exits — the
    parent's alias-index cache for this workspace is invalidated, because
    the child's writes bypassed in-process invalidation.

    A watchdog thread samples the worker tree's RSS and terminates the whole
    process group above the cap (``max_rss_mb``; None/0 = a fraction of total
    RAM) — a runaway dream must die alone instead of dragging the host into
    swap (2026-07-18 incident). A killed dream is retried by the next
    trigger/cron; the per-session cursors make that safe.
    """
    import time

    argv = _worker_argv(mode, trigger, Path(workspace))
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    logger.info(
        "dream worker spawned (pid={} mode={} trigger={})", proc.pid, mode, trigger
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

    cap_mb = _resolve_rss_cap_mb(max_rss_mb)
    watchdog_stop = threading.Event()
    watchdog_kill: dict[str, float] = {}

    def _watchdog() -> None:
        from durin.telemetry.logger import bind_telemetry, get_session_logger
        from durin.utils.process_tree import tree_rss_mb

        # This thread emits the rss_kill event; without a bound logger
        # emit_tool_event drops it.
        bind_telemetry(get_session_logger("dream_supervisor"))
        while not watchdog_stop.wait(_WATCHDOG_INTERVAL_S):
            if proc.poll() is not None:
                return
            rss, children = tree_rss_mb(proc.pid)
            total = rss + children
            if total > cap_mb:
                watchdog_kill["rss_mb"] = total
                logger.warning(
                    "dream worker over RSS cap — terminating tree "
                    "(pid={} rss={}MB children={}MB cap={}MB trigger={})",
                    proc.pid, rss, children, cap_mb, trigger,
                )
                try:
                    from durin.agent.tools._telemetry import emit_tool_event

                    emit_tool_event("memory.dream.rss_kill", {
                        "trigger": trigger, "mode": mode,
                        "rss_mb": total, "cap_mb": cap_mb,
                    })
                except Exception:  # noqa: BLE001 - telemetry best-effort
                    pass
                _terminate_worker_tree(proc)
                return

    if cap_mb > 0:
        threading.Thread(
            target=_watchdog, daemon=True, name=f"dream-{trigger}-watchdog",
        ).start()
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
        logger.info(
            "dream worker exited (pid={} code={} mode={} trigger={} {}ms{})",
            proc.pid, code, mode, trigger,
            int((time.perf_counter() - t0) * 1000),
            f" rss_killed_at={watchdog_kill['rss_mb']}MB" if watchdog_kill else "",
        )
    finally:
        watchdog_stop.set()
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
        _terminate_worker_tree(proc, grace_s=max(grace_s, 0.1))
