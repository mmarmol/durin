"""Cross-process lost-update test for CronService.

Two processes simultaneously call add_job on the same jobs.json while
_running=True.  Without a FileLock across the read-modify-write, one add
clobbers the other.

The race is exposed by patching _load_store to sleep *after* loading
(so both processes have read the empty store before either writes), which
guarantees the clobber even on fast machines.  After the fix, self._lock
serialises the sequence and both jobs survive.
"""

import multiprocessing as mp
import os
import time
from pathlib import Path


def _add_running(home: str, jobs_dir: str, name: str, ready_file: str) -> None:
    """Add a job via CronService with _running=True (the racy branch).

    _load_store is wrapped to (a) signal readiness and (b) sleep until
    both processes have loaded, maximising the race window.
    """
    os.environ["DURIN_HOME"] = home
    from durin.cron.types import CronSchedule
    from durin.cron.service import CronService

    store_path = Path(jobs_dir) / "jobs.json"
    svc = CronService(store_path)
    svc._running = True
    svc._arm_timer = lambda: None  # no event loop in subprocess

    original_load = svc._load_store

    def _load_then_wait():
        result = original_load()
        # Signal that we have loaded
        (Path(ready_file).parent / f"ready_{name}").touch()
        # Wait until the peer has also loaded (both hold the old snapshot)
        deadline = time.monotonic() + 10.0
        while not (Path(ready_file).parent / "ready_job0").exists() or \
              not (Path(ready_file).parent / "ready_job1").exists():
            time.sleep(0.01)
            if time.monotonic() > deadline:
                break
        return result

    svc._load_store = _load_then_wait

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
    ready_sentinel = str(ready_dir / "sentinel")

    ctx = mp.get_context("spawn")
    processes = [
        ctx.Process(
            target=_add_running,
            args=(str(tmp_path), str(jobs_dir), f"job{i}", ready_sentinel),
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
