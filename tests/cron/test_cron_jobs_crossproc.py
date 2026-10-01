"""Cross-process lost-update test for CronService.

Two processes simultaneously call add_job on the same jobs.json while
_running=True.  Without a FileLock across the read-modify-write, one add
clobbers the other.

The race is exposed by patching _load_store to wait *after* loading until
the peer has loaded too (so both hold the empty store before either writes),
which guarantees the clobber even on fast machines.  After the fix,
self._lock serialises the sequence and both jobs survive.
"""

import multiprocessing as mp
import os
import time
from pathlib import Path

# How long a process that has loaded the store waits for its peer to load
# too, once the peer has announced it is about to add. Without the lock the
# peer loads within milliseconds of that announcement; with it the peer is
# blocked on the lock and this grace simply runs out.
_PEER_LOAD_GRACE_S = 0.5


def _add_running(home: str, jobs_dir: str, name: str, ready_dir: str) -> None:
    """Add a job via CronService with _running=True (the racy branch).

    Each process announces, right before add_job, that it is about to add.
    _load_store is wrapped to (a) signal that this process loaded and (b) wait
    for the peer's announcement (spawn and import time vary), then give the
    peer a short grace to load as well, maximising the race window.
    """
    os.environ["DURIN_HOME"] = home
    from durin.cron.service import CronService
    from durin.cron.types import CronSchedule

    ready = Path(ready_dir)
    store_path = Path(jobs_dir) / "jobs.json"
    svc = CronService(store_path)
    svc._running = True
    svc._arm_timer = lambda: None  # no event loop in subprocess

    original_load = svc._load_store

    def _both(prefix: str) -> bool:
        return all((ready / f"{prefix}_job{i}").exists() for i in range(2))

    def _load_then_wait():
        result = original_load()
        (ready / f"loaded_{name}").touch()
        deadline = time.monotonic() + 10.0
        while not _both("about") and time.monotonic() < deadline:
            time.sleep(0.005)
        grace = time.monotonic() + _PEER_LOAD_GRACE_S
        while not _both("loaded") and time.monotonic() < grace:
            time.sleep(0.005)
        return result

    svc._load_store = _load_then_wait
    (ready / f"about_{name}").touch()
    svc.add_job(
        name=name,
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message=f"task for {name}",
    )


def test_two_processes_no_lost_job(tmp_path: Path) -> None:
    """Both concurrently-added jobs must survive in jobs.json.

    Runs the add_job path that does _load_store → mutate → _save_store
    (_running=True branch) from two processes in lock-step to make the
    lost-update race deterministic.  With self._lock held across that
    sequence, the lock serialises the two processes and both jobs survive.

    Cross-process lock ordering: CronService mutators hold self._lock across
    the full load→mutate→save sequence to prevent lost-update races.
    """
    jobs_dir = tmp_path / "cron"
    jobs_dir.mkdir()
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()

    ctx = mp.get_context("spawn")
    processes = [
        ctx.Process(
            target=_add_running,
            args=(str(tmp_path), str(jobs_dir), f"job{i}", str(ready_dir)),
        )
        for i in range(2)
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(20)
        assert p.exitcode == 0, f"process exited with code {p.exitcode}"

    from durin.cron.service import CronService

    svc = CronService(jobs_dir / "jobs.json")
    jobs = svc.list_jobs(include_disabled=True)
    names = {j.name for j in jobs}
    assert names == {"job0", "job1"}, (
        f"Expected both jobs to survive; got: {names!r}. "
        "A lost-update race dropped one job."
    )
