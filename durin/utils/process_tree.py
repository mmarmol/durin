"""Resident-set sampling for a process and its descendants.

One `ps` invocation snapshots pid/ppid/rss for the whole system and the
tree is resolved in-process, so the helpers work unchanged on Linux and
macOS without psutil. Intended for coarse waypoints (dream pass
boundaries, a supervisor watchdog tick) — not per-allocation profiling.

Also the identity of a process (pid plus a start identity that does not move
while it lives), deciding that a process is gone only on proof, and ending a
recorded script's process group only while that is proven to be the script.
"""
from __future__ import annotations

import os
import subprocess
import uuid

__all__ = [
    "BOOT_ID",
    "available_memory_mb",
    "ObservationError",
    "end_script_groups",
    "left_script_state",
    "memory_snapshot",
    "process_alive",
    "process_identity",
    "process_rss_mb",
    "process_state",
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


class ObservationError(Exception):
    """A process could not be observed: neither its presence nor its absence is
    known. Never read as "gone"."""


# The start identity of a process is compared for equality only, never parsed.
# Where /proc is readable it is ``proc:<boot id>:<start ticks>``: the kernel's
# boot id and the process's start time in clock ticks since boot. Neither moves
# while the process lives, whatever happens to the wall clock (``ps lstart`` is
# derived from the boot time, which moves when the clock is stepped). Elsewhere
# (macOS) it is the ``ps -o lstart`` string, the form every identity had
# before; a recorded value in that form is still compared the old way.
_PROC_FORM = "proc:"
_PS_TIMEOUT_S = 5.0


def _read_boot_id() -> str:
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
        return fh.read().strip()


def _read_proc_stat(pid: int) -> str:
    """The ``/proc/<pid>/stat`` line. FileNotFoundError when there is no such process."""
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _list_proc_pids() -> list[int]:
    return [int(name) for name in os.listdir("/proc") if name.isdigit()]


def _proc_boot_id() -> str | None:
    """The kernel boot id when /proc is readable here, else None."""
    try:
        return _read_boot_id() or None
    except OSError:
        return None


def _parse_proc_stat(line: str) -> tuple[str, int, int]:
    """``(state, process group id, start ticks)`` from a ``/proc/<pid>/stat``
    line. The command name sits between the first ``(`` and the LAST ``)`` and
    may itself hold spaces and parentheses, so the fields are counted from the
    last ``)``. Raises ValueError on a line of another shape."""
    end = line.rindex(")")
    fields = line[end + 1:].split()
    if len(fields) < 20:
        raise ValueError("a /proc stat line with too few fields")
    return fields[0], int(fields[2]), int(fields[19])


class Seen:
    """One observation of a process: pid, process group, whether it has exited
    unreaped (a zombie), and its start identity in this system's form."""

    __slots__ = ("pgid", "pid", "started", "zombie")

    def __init__(self, pid: int, pgid: int, zombie: bool, started: str) -> None:
        self.pid, self.pgid, self.zombie, self.started = pid, pgid, zombie, started

    def identity(self) -> dict:
        return {"pid": self.pid, "started": self.started}


def _ps(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["ps", *args], capture_output=True, text=True,
                              timeout=max(0.05, timeout))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ObservationError(f"ps failed: {exc}") from exc


def _no_such_process(pid: int) -> bool:
    """Proof of absence: signal 0 finds no process with that pid. A zombie
    still has its pid, so it is not absent here."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _from_ps_line(text: str) -> tuple[str, int, str]:
    """``(state, pgid, lstart)`` from a ``stat= pgid= lstart=`` ps line."""
    state, pgid, rest = text.split(None, 2)
    return state, int(pgid), rest.strip()


def _observe(pid: int, *, timeout: float = _PS_TIMEOUT_S) -> Seen | None:
    """Observe one process: a ``Seen``, or None on proof that no process has
    that pid. Raises ObservationError when neither is known."""
    boot = _proc_boot_id()
    if boot is not None:
        try:
            line = _read_proc_stat(pid)
        except (FileNotFoundError, ProcessLookupError):
            # The file can be missing for other reasons (a /proc mounted with
            # hidepid hides other users' processes): gone only when signal 0
            # agrees, as on macOS.
            if _no_such_process(pid):
                return None
            raise ObservationError(f"/proc/{pid}/stat is missing for a live pid") from None
        except OSError as exc:
            raise ObservationError(f"/proc/{pid}/stat: {exc}") from exc
        try:
            state, pgid, ticks = _parse_proc_stat(line)
        except (ValueError, IndexError) as exc:
            raise ObservationError(f"/proc/{pid}/stat: {exc}") from exc
        return Seen(pid, pgid, state in ("Z", "X"), f"{_PROC_FORM}{boot}:{ticks}")
    out = _ps(["-p", str(pid), "-o", "stat=,pgid=,lstart="], timeout)
    text = out.stdout.strip()
    if not text:
        # ps prints nothing both for a pid that does not exist and when it fails.
        if _no_such_process(pid):
            return None
        raise ObservationError(f"ps printed nothing for live pid {pid}")
    try:
        state, pgid, started = _from_ps_line(text)
    except ValueError as exc:
        raise ObservationError(f"ps line {text!r}: {exc}") from exc
    return Seen(pid, pgid, state.startswith("Z"), started)


def _lstart_of(pid: int, timeout: float) -> str | None:
    """The ``ps -o lstart`` string of ``pid``, None on proof it does not exist.
    Raises ObservationError otherwise."""
    out = _ps(["-p", str(pid), "-o", "lstart="], timeout)
    text = out.stdout.strip()
    if text:
        return text
    if _no_such_process(pid):
        return None
    raise ObservationError(f"ps printed nothing for live pid {pid}")


def _same_start(seen: Seen, recorded: str, *, timeout: float = _PS_TIMEOUT_S) -> bool:
    """Whether the observed process is the one whose start identity was
    recorded. A value recorded in the ``ps lstart`` form where this system
    reads /proc is compared the old way. Raises ObservationError when it cannot
    be decided."""
    if recorded.startswith(_PROC_FORM) == seen.started.startswith(_PROC_FORM):
        return seen.started == recorded
    if not recorded.startswith(_PROC_FORM):
        current = _lstart_of(seen.pid, timeout)
        return current is not None and current == recorded
    raise ObservationError("a /proc start identity cannot be read on this system")


def process_identity(pid: int | None = None) -> dict:
    """A durable identity for a live process: ``{"pid", "started", "boot"}``.

    Persisted by run manifests so a later process can tell "this run's owner
    is still alive" from "the pid was reused by something else" or "the same
    process restarted in place". ``boot`` is this start of the program.
    ``started`` is None when the process cannot be observed.
    """
    pid = pid if pid is not None else _own_pid()
    try:
        seen = _observe(pid)
    except ObservationError:
        seen = None
    started = seen.started if seen is not None and not seen.zombie else None
    return {"pid": pid, "started": started, "boot": BOOT_ID}


def process_alive(owner: object) -> bool:
    """False only on proof that the process recorded in *owner* is gone.

    Proof is: no valid pid; this process's own pid recorded with a different
    ``boot`` (an earlier start of this program, since a restart in place
    keeps pid and start time); no process with that pid; a process with that
    pid that has exited (a zombie); or a process with that pid and another
    start identity (the pid was reused). An owner recorded without a start
    time is judged by its pid alone. Anything that cannot be observed (``ps``
    failing or timing out, an unreadable /proc) counts as alive: a caller
    that would end or take over the owner's work must never do so on a
    guess.
    """
    if not isinstance(owner, dict):
        return False
    try:
        pid = int(owner.get("pid"))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    boot = owner.get("boot")
    if pid == _own_pid() and boot is not None and boot != BOOT_ID:
        return False
    try:
        seen = _observe(pid)
    except ObservationError:
        return True
    if seen is None or seen.zombie:
        return False
    recorded = owner.get("started")
    if not isinstance(recorded, str) or not recorded:
        return True
    try:
        return _same_start(seen, recorded)
    except ObservationError:
        return True


def process_state(pid: object, started: object) -> str:
    """``"alive"`` while ``pid`` is the process started at ``started`` and has
    not exited, ``"gone"`` on proof it is not (no such process, a zombie, or
    another start identity), ``"unknown"`` when that cannot be observed. A
    value that names no process (no integer pid, no start identity) is
    ``"gone"``: there is nothing it could still be."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "gone"
    if not isinstance(started, str) or not started:
        return "gone"
    try:
        seen = _observe(pid)
        if seen is None or seen.zombie:
            return "gone"
        return "alive" if _same_start(seen, started) else "gone"
    except ObservationError:
        return "unknown"


def _process_table(timeout: float) -> list[Seen]:
    """Every visible process, observed at once. Raises ObservationError when the
    table cannot be read whole."""
    boot = _proc_boot_id()
    if boot is not None:
        try:
            pids = _list_proc_pids()
        except OSError as exc:
            raise ObservationError(f"/proc: {exc}") from exc
        table: list[Seen] = []
        for pid in pids:
            try:
                line = _read_proc_stat(pid)
            except (FileNotFoundError, ProcessLookupError):
                continue   # exited while the table was read
            except PermissionError:
                continue   # another user's process: not a script of this one's
            except OSError as exc:
                raise ObservationError(f"/proc/{pid}/stat: {exc}") from exc
            try:
                state, pgid, ticks = _parse_proc_stat(line)
            except (ValueError, IndexError) as exc:
                raise ObservationError(f"/proc/{pid}/stat: {exc}") from exc
            table.append(Seen(pid, pgid, state in ("Z", "X"), f"{_PROC_FORM}{boot}:{ticks}"))
        return table
    out = _ps(["-A", "-o", "pid=,pgid=,stat=,lstart="], timeout)
    if out.returncode != 0 or not out.stdout.strip():
        raise ObservationError(f"ps -A failed ({out.returncode})")
    table = []
    for raw in out.stdout.splitlines():
        if not raw.strip():
            continue
        try:
            pid, rest = raw.split(None, 1)
            state, pgid, started = _from_ps_line(_reorder(rest))
            table.append(Seen(int(pid), pgid, state.startswith("Z"), started))
        except ValueError as exc:
            raise ObservationError(f"ps line {raw!r}: {exc}") from exc
    return table


def _reorder(rest: str) -> str:
    """``pgid stat lstart`` (the table's column order) as ``stat pgid lstart``."""
    pgid, state, started = rest.split(None, 2)
    return f"{state} {pgid} {started}"


def _protected(pid: int) -> bool:
    """A pid nothing here may ever signal, as a process or as a group: 0 and
    1, this process, and the group this process belongs to."""
    if pid <= 1 or pid == _own_pid():
        return True
    try:
        return pid == os.getpgrp()
    except OSError:
        return True


def _group_empty(pgid: int, members: list[Seen]) -> bool:
    """Proof that group ``pgid`` has no member that has not exited. ``members``
    are the group's members in one table read (zombies included). Raises
    ObservationError when the table and the system disagree."""
    if any(not m.zombie for m in members):
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # macOS answers EPERM for a group holding only zombies; with no member
        # in the table, the group belongs to no one this process can see.
        if members:
            return True
        raise ObservationError(f"group {pgid} exists but no member of it is visible") from None
    except OSError as exc:
        raise ObservationError(f"group {pgid}: {exc}") from exc
    if members:
        return True    # every member has exited; their parents have not reaped them
    raise ObservationError(f"group {pgid} exists but no member of it is visible")


def _reap_group(pgid: int) -> None:
    """Collect the exit status of this process's own children in group
    ``pgid`` (a process restarted in place still has its earlier start's
    children), so none lingers as a zombie. Others' children are left to
    their parents."""
    while True:
        try:
            pid, _status = os.waitpid(-pgid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return
        if pid == 0:
            return


def _verify_and_signal(pid: int, started: str, sig: int, timeout: float) -> bool:
    """Send ``sig`` to the group ``pid`` leads, only after checking, right here,
    that ``pid`` is alive, is the process started at ``started`` and leads its
    own group. Returns whether the signal was sent."""
    if _protected(pid):
        return False
    try:
        seen = _observe(pid, timeout=timeout)
        if seen is None or seen.zombie or not _same_start(seen, started, timeout=timeout):
            return False
        if os.getpgid(pid) != pid:
            return False
        os.killpg(pid, sig)
    except (ObservationError, OSError):
        return False
    return True


def end_script_groups(scripts: list[tuple[int, str]], *, term_wait_s: float,
                      kill_wait_s: float, bound_s: float,
                      poll_s: float = 0.05) -> list[dict]:
    """End the process groups that recorded script processes lead, and say for
    each whether nothing of it is left. ``scripts`` are ``(pid, started)``
    pairs, each a process started in a session of its own (its pid is its
    group id). Returns, per script, ``{"ended", "members", "unchecked"}``:
    ``ended`` only on proof that the group has no member that has not exited;
    otherwise ``members`` are the identities still seen in it (``{"pid",
    "started"}``) and ``unchecked`` says an observation failed.

    A group is signalled only when its leader was verified, in the same call
    as the signal, to be alive, to be the recorded process and to lead its own
    group: SIGTERM first, SIGKILL after ``term_wait_s`` to a group that still
    has members, then up to ``kill_wait_s`` more. SIGKILL goes to the group
    only when the poll made right before it observed the group and saw in it a
    member seen before SIGTERM, which proves the group never emptied (an
    emptied group's id can be given to another process); when that poll could
    not observe the group, nothing is sent and the script is reported. Never signalled: a pid of 0 or 1, this process or its group; a
    leader that is gone, has exited, or left its group (its members are
    reported, not signalled: nothing proves they are still the script's); a
    pid now held by another process (then nothing of the script is left: a pid
    is never reused while a group with its id has members).

    Everything, observations included, happens within ``bound_s`` of the
    call: each observation's own timeout is cut to the time left, and a
    script still undecided at the bound is reported ``unchecked``."""
    import signal
    import time

    start = time.monotonic()
    deadline = start + bound_s

    def left_s() -> float:
        return deadline - time.monotonic()

    results = [{"ended": False, "unchecked": True,
                "members": [{"pid": pid, "started": started}]} for pid, started in scripts]
    try:
        table = _process_table(left_s())
    except ObservationError:
        return results

    # Groups this call signalled: index -> (pgid, identities seen before SIGTERM).
    followed: dict[int, tuple[int, set[tuple[int, str]]]] = {}
    # Per followed group, the live members the latest poll saw, or None when the
    # latest poll could not observe the group: SIGKILL needs a fresh observation.
    fresh: dict[int, set[tuple[int, str]] | None] = {}
    for i, (pid, started) in enumerate(scripts):
        members = [m for m in table if m.pgid == pid]
        live = [m for m in members if not m.zombie]
        leader = next((m for m in table if m.pid == pid), None)
        try:
            if leader is not None and not leader.zombie:
                if not _same_start(leader, started, timeout=left_s()):
                    # The pid now belongs to another process, so the script's
                    # group had emptied before it was given out.
                    results[i] = {"ended": True, "unchecked": False, "members": []}
                    continue
                if _protected(pid) or leader.pgid != pid:
                    # Never signalled: this process or its group, or a leader
                    # that left its group (the group is no longer what it led).
                    results[i] = {"ended": False, "unchecked": False,
                                  "members": [leader.identity(),
                                              *(m.identity() for m in live if m.pid != pid)]}
                    continue
                if _verify_and_signal(pid, started, signal.SIGTERM, left_s()):
                    followed[i] = (pid, {(m.pid, m.started) for m in live})
                    fresh[i] = None
                else:
                    results[i] = {"ended": False, "unchecked": True,
                                  "members": [m.identity() for m in live] or results[i]["members"]}
                continue
            if live:
                # The leader is gone and members remain: nothing proves they are
                # still the script's, so they are reported and never signalled.
                results[i] = {"ended": False, "unchecked": False,
                              "members": [m.identity() for m in live]}
            elif _group_empty(pid, members):
                results[i] = {"ended": True, "unchecked": False, "members": []}
        except ObservationError:
            results[i] = {"ended": False, "unchecked": True,
                          "members": [m.identity() for m in live] or results[i]["members"]}

    for sig, wait_s in ((None, term_wait_s), (signal.SIGKILL, kill_wait_s)):
        if not followed:
            break
        if sig is not None:
            for i, (pgid, before) in list(followed.items()):
                still = fresh.get(i)
                if still is None:
                    # The latest poll could not observe the group: nothing
                    # proves it is still the script's, so nothing is sent.
                    results[i]["unchecked"] = True
                    del followed[i]
                    continue
                if not (still & before) or _protected(pgid):
                    # The group may have emptied and its id been given out:
                    # nothing here signals it again.
                    results[i]["unchecked"] = False
                    del followed[i]
                    continue
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    pass
                except OSError:
                    results[i]["unchecked"] = False
                    del followed[i]
        phase_end = min(time.monotonic() + wait_s, deadline)
        while followed:
            for pgid, _before in followed.values():
                _reap_group(pgid)
            try:
                table = _process_table(left_s())
            except ObservationError:
                for i in followed:
                    results[i]["unchecked"] = True
                followed.clear()
                break
            for i, (pgid, _before) in list(followed.items()):
                members = [m for m in table if m.pgid == pgid]
                try:
                    empty = _group_empty(pgid, members)
                except ObservationError:
                    results[i]["unchecked"] = True
                    fresh[i] = None
                    continue
                if empty:
                    results[i] = {"ended": True, "unchecked": False, "members": []}
                    del followed[i]
                else:
                    live = [m for m in members if not m.zombie]
                    results[i] = {"ended": False, "unchecked": False,
                                  "members": [m.identity() for m in live]}
                    fresh[i] = {(m.pid, m.started) for m in live}
            remaining = phase_end - time.monotonic()
            if not followed or remaining <= 0:
                break
            time.sleep(min(poll_s, remaining))
    return results


def left_script_state(pid: int, started: str, members: list[dict], *,
                      timeout: float = _PS_TIMEOUT_S) -> tuple[str, list[int], list[int]]:
    """Whether a script a dead run left could still be running, as
    ``(state, own, group)``:

    - ``"alive"`` while the recorded script process itself (``pid`` with
      ``started``) is still that process (its pid is in ``own``), or another
      recorded member is still that process, or the group the script led still
      has a member, unless the script's pid is now another process's, which
      proves the group emptied (those pids are in ``group``: they are in the
      script's former process group, which does not prove they are its);
    - ``"unknown"`` when that cannot be observed (``group`` holds the recorded
      pids);
    - ``"gone"`` on proof that nothing of it is left.

    Only the recorded identities and the group are looked at, so a reused pid
    never keeps it alive."""
    own: list[int] = []
    group: list[int] = []
    unknown = False
    for m in members:
        if not isinstance(m, dict):
            continue
        state = process_state(m.get("pid"), m.get("started"))
        if state == "alive":
            if m.get("pid") == pid and m.get("started") == started:
                own.append(pid)
            else:
                group.append(m["pid"])
        elif state == "unknown":
            unknown = True
    try:
        leader = _observe(pid, timeout=timeout)
        reused = (leader is not None and not leader.zombie
                  and not _same_start(leader, started, timeout=timeout))
        if not reused:
            if leader is not None and not leader.zombie and pid not in own:
                own.append(pid)
            in_group = [m for m in _process_table(timeout) if m.pgid == pid]
            live = [m.pid for m in in_group if not m.zombie]
            group.extend(p for p in live if p != pid)
            if not live and not _group_empty(pid, in_group):
                unknown = True
    except ObservationError:
        unknown = True
    if own or group:
        return "alive", sorted(set(own)), sorted(set(group) - set(own))
    if unknown:
        recorded = {m["pid"] for m in members
                    if isinstance(m, dict) and isinstance(m.get("pid"), int)} or {pid}
        return "unknown", [], sorted(recorded)
    return "gone", [], []


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
