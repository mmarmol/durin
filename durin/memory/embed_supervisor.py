"""Gateway-side supervision of the standing embedding server.

The gateway owns the server's lifecycle: spawn at boot, respawn on death
(with backoff, giving up after repeated instant exits — e.g. the [memory]
extra missing), RSS-cap restart (the supervised restart IS the arena-reclaim
mechanism for a process that holds the ONNX model inline), and teardown on
gateway shutdown. Consumers never talk to this module — they find the server
through the discovery file it maintains.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from typing import Any

from loguru import logger

# Consecutive sub-_FAST_EXIT_S exits before the supervisor gives up — a
# missing extra or broken install would otherwise respawn forever.
_GIVE_UP_AFTER = 3
_FAST_EXIT_S = 10.0
_RESPAWN_BACKOFF_S = 5.0
_WATCHDOG_INTERVAL_S = 10.0
# Auto RSS cap: fraction of total RAM, floored so small hosts still fit the
# model + arena comfortably.
_AUTO_RSS_FRACTION = 0.25
_AUTO_RSS_FLOOR_MB = 1024.0

_state_lock = threading.Lock()
_proc: subprocess.Popen | None = None
# Per-start stop event + thread handle: each start_* call owns a fresh event,
# so a stop/start cycle can never resurrect a previous supervisor loop.
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _server_argv(port: int) -> list[str]:
    return [
        sys.executable, "-m", "durin", "memory", "embed-server",
        "--port", str(port),
    ]


def _resolve_cap_mb(max_rss_mb: int) -> float:
    if max_rss_mb and max_rss_mb > 0:
        return float(max_rss_mb)
    from durin.utils.process_tree import total_memory_mb

    total = total_memory_mb()
    return max(total * _AUTO_RSS_FRACTION, _AUTO_RSS_FLOOR_MB) if total else 0.0


def start_embed_server_supervisor(config: Any) -> bool:
    """Start the supervisor thread (once per process). Returns False when
    the config rules the service out (memory disabled, isolation not
    "service") or it is already running."""
    global _stop_event, _thread
    memory_cfg = getattr(config, "memory", None)
    embedding_cfg = getattr(memory_cfg, "embedding", None)
    if not getattr(memory_cfg, "enabled", False):
        return False
    if getattr(embedding_cfg, "isolation", "service") != "service":
        return False
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            return False
        stop_event = threading.Event()
        _stop_event = stop_event

    port = int(getattr(embedding_cfg, "service_port", 0) or 0)
    cap_mb = _resolve_cap_mb(int(getattr(embedding_cfg, "service_max_rss_mb", 0) or 0))

    def _supervise() -> None:
        global _proc
        import time

        from durin.memory.dream_supervisor import _terminate_worker_tree
        from durin.memory.embed_server import clear_discovery
        from durin.utils.process_tree import tree_rss_mb

        fast_exits = 0
        while not stop_event.is_set():
            t_start = time.monotonic()
            try:
                proc = subprocess.Popen(
                    _server_argv(port),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                logger.exception("embed server spawn failed")
                return
            with _state_lock:
                _proc = proc
            logger.info("embed server spawned (pid={})", proc.pid)

            while not stop_event.is_set() and proc.poll() is None:
                stop_event.wait(_WATCHDOG_INTERVAL_S)
                if proc.poll() is not None or stop_event.is_set():
                    break
                if cap_mb > 0:
                    rss, children = tree_rss_mb(proc.pid)
                    if rss + children > cap_mb:
                        logger.warning(
                            "embed server over RSS cap — restarting "
                            "(rss={}MB cap={}MB)", rss + children, cap_mb)
                        _terminate_worker_tree(proc)

            if stop_event.is_set():
                _terminate_worker_tree(proc)
                clear_discovery()
                return

            lived_s = time.monotonic() - t_start
            code = proc.poll()
            clear_discovery()
            if lived_s < _FAST_EXIT_S:
                fast_exits += 1
                if fast_exits >= _GIVE_UP_AFTER:
                    logger.error(
                        "embed server exited {} times within {}s of spawn "
                        "(last code={}) — giving up; embedding falls back to "
                        "per-process pools", fast_exits, _FAST_EXIT_S, code)
                    return
            else:
                fast_exits = 0
            logger.warning(
                "embed server exited (code={} after {:.0f}s) — respawning",
                code, lived_s)
            stop_event.wait(_RESPAWN_BACKOFF_S)

    thread = threading.Thread(
        target=_supervise, daemon=True, name="embed-server-supervisor")
    with _state_lock:
        _thread = thread
    thread.start()
    return True


def stop_embed_server(grace_s: float = 10.0) -> None:
    """Gateway shutdown: stop the supervisor loop and terminate the server."""
    _stop_event.set()
    with _state_lock:
        proc = _proc
        thread = _thread
    if proc is not None and proc.poll() is None:
        from durin.memory.dream_supervisor import _terminate_worker_tree

        _terminate_worker_tree(proc, grace_s=grace_s)
    if thread is not None:
        thread.join(timeout=grace_s + 5.0)
    from durin.memory.embed_server import clear_discovery

    clear_discovery()
