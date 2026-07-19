"""Resident-set sampling for a process and its descendants.

One `ps` invocation snapshots pid/ppid/rss for the whole system and the
tree is resolved in-process, so the helpers work unchanged on Linux and
macOS without psutil. Intended for coarse waypoints (dream pass
boundaries, a supervisor watchdog tick) — not per-allocation profiling.
"""
from __future__ import annotations

import os
import subprocess

__all__ = [
    "available_memory_mb",
    "process_rss_mb",
    "total_memory_mb",
    "tree_rss_mb",
]


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
