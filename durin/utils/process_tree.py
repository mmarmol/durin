"""Resident-set sampling for a process and its descendants.

One `ps` invocation snapshots pid/ppid/rss for the whole system and the
tree is resolved in-process, so the helpers work unchanged on Linux and
macOS without psutil. Intended for coarse waypoints (dream pass
boundaries, a supervisor watchdog tick) — not per-allocation profiling.

Also the identity of a process (pid plus start time), and ending a recorded
process group only while its leader is still the process that was recorded.
"""
from __future__ import annotations

import os
import subprocess
import uuid

__all__ = [
    "BOOT_ID",
    "available_memory_mb",
    "end_process_groups",
    "memory_snapshot",
    "process_alive",
    "process_identity",
    "process_rss_mb",
    "same_live_process",
    "total_memory_mb",
    "tree_rss_mb",
]


def memory_snapshot() -> dict:
    """One cheap footprint snapshot of THIS process for telemetry/diagnostics:
    resident set, direct+transitive children total, thread count, gc gen
    sizes, the glibc allocator's live-vs-retained split and how much of it is
    in RAM, the resident memory outside that allocator, and the host's
    total/available memory for context."""
    import gc
    import threading

    from durin.utils.glibc_malloc import malloc_stats_mb

    rss, children = tree_rss_mb()
    # 0.0 = unknown (not glibc), same convention as available_memory_mb.
    # rss_mb far above malloc_in_use_mb distinguishes allocator retention
    # from genuine object growth without attaching a debugger.
    malloc = malloc_stats_mb() or {
        "system_mb": 0.0, "in_use_mb": 0.0, "free_mb": 0.0, "resident_mb": 0.0}
    # Resident memory glibc malloc does not account for: CPython's object
    # arenas (mapped directly, not malloc'd), native libraries that map their
    # own memory, thread stacks, mapped code. rss minus the arenas' resident
    # part, not minus malloc_system_mb: that keeps counting the pages a trim
    # returned, so it would hide this much growth after every trim.
    non_malloc = (
        round(max(0.0, rss - malloc["resident_mb"]), 1)
        if malloc["resident_mb"] > 0.0 else 0.0)
    return {
        "rss_mb": rss,
        "children_mb": children,
        "threads": threading.active_count(),
        "gc_counts": list(gc.get_count()),
        "malloc_system_mb": malloc["system_mb"],
        "malloc_in_use_mb": malloc["in_use_mb"],
        "malloc_free_mb": malloc["free_mb"],
        "malloc_resident_mb": malloc["resident_mb"],
        "non_malloc_mb": non_malloc,
        "total_mb": total_memory_mb(),
        "available_mb": available_memory_mb(),
    }


# One random value per start of the program. A process that replaces its own
# image (os.execv) keeps its pid and its start time but re-imports this module,
# so this is what tells the restarted process from the one it replaced.
BOOT_ID = uuid.uuid4().hex


def _own_pid() -> int:
    return os.getpid()


def _lstart(pid: int) -> str | None:
    """The process' start-time string from ps, or None when it doesn't exist.

    The string is compared for EQUALITY only (never parsed), so locale and
    format don't matter — it just has to be stable for a given process,
    which `ps -o lstart` is on both Linux and macOS.
    """
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    return text or None


def process_identity(pid: int | None = None) -> dict:
    """A durable identity for a live process: ``{"pid", "started", "boot"}``.

    Persisted by run manifests so a later process can tell "this run's owner
    is still alive" from "the pid was reused by something else" or "the same
    process restarted in place". ``boot`` is this start of the program.
    """
    pid = pid if pid is not None else _own_pid()
    return {"pid": pid, "started": _lstart(pid), "boot": BOOT_ID}


def process_alive(owner: object) -> bool:
    """True when the process recorded in *owner* is still the same, live
    process. A missing/invalid pid is dead; a live pid with a DIFFERENT
    start time is dead (pid reuse); an owner recorded without a start time
    falls back to pid-liveness alone. This process's own pid recorded with a
    different ``boot`` is an earlier start of this program (a restart in
    place keeps pid and start time), so it is dead; an owner without a
    ``boot`` is judged as before.
    """
    if not isinstance(owner, dict):
        return False
    try:
        pid = int(owner.get("pid"))
    except (TypeError, ValueError):
        return False
    boot = owner.get("boot")
    if pid == _own_pid() and boot is not None and boot != BOOT_ID:
        return False
    current = _lstart(pid)
    if current is None:
        return False
    recorded = owner.get("started")
    return recorded is None or current == recorded


def _zombie(pid: int) -> bool:
    """Whether ``pid`` is a process that has exited but not been reaped."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip().startswith("Z")


def same_live_process(pid: object, started: object) -> bool:
    """True when ``pid`` is still the process that had start time ``started``
    and has not exited. A recorded identity without a start time cannot be
    told from a later process given the same pid, so it never counts."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not isinstance(started, str) or not started:
        return False
    return _lstart(pid) == started and not _zombie(pid)


def _protected(pid: int) -> bool:
    """A pid nothing here may ever signal: 0 and 1, this process, and the
    group this process belongs to."""
    if pid <= 1 or pid == _own_pid():
        return True
    try:
        return pid == os.getpgrp()
    except OSError:
        return True


def _signal_group(pid: int, started: str, sig: int) -> None:
    """Send ``sig`` to the process group led by ``pid``, only after checking,
    immediately before, that ``pid`` is still the process started at
    ``started``. A process that left its group is signalled alone."""
    if _protected(pid) or not same_live_process(pid, started):
        return
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    except PermissionError:
        pass


def _reap(pid: int) -> None:
    """Collect ``pid``'s exit status when it is a child of this process (a
    process restarted in place still has its earlier start's children), so it
    does not linger as a zombie. Anyone else's child is left to its parent."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def end_process_groups(members: list[tuple[int, str]], *, term_wait_s: float,
                       kill_wait_s: float, poll_s: float = 0.05) -> set[int]:
    """End the process groups led by ``members`` (``(pid, started)`` pairs, each
    a group leader started with a session of its own): SIGTERM, up to
    ``term_wait_s`` for them to go, then SIGKILL for the rest and up to
    ``kill_wait_s`` more. Each signal goes only to a leader that is still the
    recorded process (see ``_signal_group``). Returns the pids still the
    recorded, live process at the end. A member that is no longer that process,
    or that nothing here may signal, is not signalled and not returned."""
    import signal
    import time

    alive = [(pid, started) for pid, started in members
             if not _protected(pid) and same_live_process(pid, started)]
    for sig, wait_s in ((signal.SIGTERM, term_wait_s), (signal.SIGKILL, kill_wait_s)):
        if not alive:
            break
        for pid, started in alive:
            _signal_group(pid, started, sig)
        deadline = time.monotonic() + wait_s
        while True:
            for pid, _started in alive:
                _reap(pid)
            alive = [(pid, started) for pid, started in alive
                     if same_live_process(pid, started)]
            if not alive or time.monotonic() >= deadline:
                break
            time.sleep(poll_s)
    return {pid for pid, _started in alive}


def _read_meminfo(field: str) -> float:
    """Return a /proc/meminfo field in MB, or 0.0 when absent (non-Linux)."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith(field + ":"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def total_memory_mb() -> float:
    """Total system RAM in MB; 0.0 when it cannot be determined."""
    mb = _read_meminfo("MemTotal")
    if mb:
        return round(mb, 1)
    try:  # macOS
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        return round(int(out) / (1024 * 1024), 1)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def available_memory_mb() -> float:
    """Memory the system could hand out without swapping, in MB.

    Linux: MemAvailable (the kernel's own reclaimable estimate). macOS: free
    + inactive pages from vm_stat. 0.0 = unknown — callers must treat that
    as "no signal", never as "no memory".
    """
    mb = _read_meminfo("MemAvailable")
    if mb:
        return round(mb, 1)
    try:  # macOS
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        page_size = 4096
        first = out.splitlines()[0] if out else ""
        if "page size of" in first:
            page_size = int(first.split("page size of")[1].split()[0])
        pages = 0
        for line in out.splitlines():
            for key in ("Pages free:", "Pages inactive:", "Pages speculative:"):
                if line.startswith(key):
                    pages += int(line.split(":")[1].strip().rstrip("."))
        return round(pages * page_size / (1024 * 1024), 1)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0.0


def _snapshot() -> dict[int, tuple[int, int]]:
    """Return {pid: (ppid, rss_kb)} for every visible process; {} on failure."""
    try:
        out = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid=,rss="],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table: dict[int, tuple[int, int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid, rss_kb = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        table[pid] = (ppid, rss_kb)
    return table


def process_rss_mb(pid: int | None = None) -> float:
    """Current RSS of one process in MB; 0.0 when unknown."""
    table = _snapshot()
    entry = table.get(pid if pid is not None else os.getpid())
    return round(entry[1] / 1024.0, 1) if entry else 0.0


def tree_rss_mb(root_pid: int | None = None) -> tuple[float, float]:
    """(root RSS, descendants RSS) in MB for *root_pid* (default: self).

    Descendants are resolved transitively from one snapshot; a process
    that exits between fork and sampling is simply absent. (0.0, 0.0)
    when the snapshot fails or the root is gone.
    """
    root = root_pid if root_pid is not None else os.getpid()
    table = _snapshot()
    if root not in table:
        return (0.0, 0.0)
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    descendants_kb = 0
    stack = list(children.get(root, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        descendants_kb += table[pid][1]
        stack.extend(children.get(pid, []))
    return (round(table[root][1] / 1024.0, 1), round(descendants_kb / 1024.0, 1))
