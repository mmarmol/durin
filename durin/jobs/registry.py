"""A registry of work too long to run inside a turn.

Generic over ``kind`` so the surface does not have to be rebuilt for the
second client; OCR of scanned documents is the first.

Progress is persisted per unit (a page, for OCR) rather than per job, which
buys two things. A worker killed at unit 380 resumes at 380. And a gateway
restart no longer loses track of running work: ``reconcile`` finds rows whose
worker process is gone and puts them back in the queue with their finished
units intact.

The database is opened through :mod:`durin.utils.sqlite_util`, so the gateway
and a worker subprocess can both write it.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from durin.utils.sqlite_util import connect, execute_write

__all__ = ["RECONCILE_AGE_S", "Job", "JobRegistry"]

# A "running" row whose pid still looks alive is assumed to be that job's own
# worker -- except once it has been running longer than this, where pid reuse
# (a rebooted host restarts pid allocation from a low number) is more likely
# than a single job legitimately taking that long. Kept independent of
# durin.workflow.run_log.RECONCILE_AGE_S (same value, same reasoning) rather
# than imported: jobs is generic infrastructure workflow runs happen to be a
# client of, not the other way around, and the two may want to diverge later.
RECONCILE_AGE_S = 6 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,
    label        TEXT NOT NULL,
    payload      TEXT NOT NULL,
    session_key  TEXT,
    units_total  INTEGER NOT NULL DEFAULT 0,
    units_done   INTEGER NOT NULL DEFAULT 0,
    pid          INTEGER,
    created_at   REAL NOT NULL,
    started_at   REAL,
    ended_at     REAL,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS jobs_session ON jobs (session_key, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);

CREATE TABLE IF NOT EXISTS job_units (
    job_id  TEXT NOT NULL,
    unit    INTEGER NOT NULL,
    text    TEXT NOT NULL,
    PRIMARY KEY (job_id, unit)
);
"""


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    status: str  # queued | running | done | failed | cancelled
    label: str
    payload: dict[str, Any]
    session_key: str | None
    units_total: int
    units_done: int
    pid: int | None
    created_at: float
    started_at: float | None
    ended_at: float | None
    error: str | None


def _row_to_job(row: Any) -> Job:
    return Job(
        id=row[0], kind=row[1], status=row[2], label=row[3],
        payload=json.loads(row[4]), session_key=row[5],
        units_total=row[6], units_done=row[7], pid=row[8],
        created_at=row[9], started_at=row[10], ended_at=row[11], error=row[12],
    )


_COLUMNS = (
    "id, kind, status, label, payload, session_key, units_total, "
    "units_done, pid, created_at, started_at, ended_at, error"
)


class JobRegistry:
    """Read/write access to the job database."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from durin.config.paths import jobs_db_path

            path = jobs_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = connect(path)
        self._conn.executescript(_SCHEMA)

    def enqueue(
        self, *, kind: str, label: str, payload: dict[str, Any],
        session_key: str | None, units_total: int,
    ) -> Job:
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        execute_write(
            self._conn,
            lambda c: c.execute(
                "INSERT INTO jobs (id, kind, status, label, payload, session_key,"
                " units_total, units_done, created_at)"
                " VALUES (?, ?, 'queued', ?, ?, ?, ?, 0, ?)",
                (job_id, kind, label, json.dumps(payload), session_key, units_total, now),
            ),
        )
        job = self.get(job_id)
        assert job is not None
        return job

    def get(self, job_id: str) -> Job | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return _row_to_job(row) if row else None

    def list_for_session(self, session_key: str) -> list[Job]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE session_key = ?"
            " ORDER BY created_at DESC, rowid DESC",
            (session_key,),
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def claim(self, job_id: str, *, pid: int) -> bool:
        """Claim a queued job for this worker process.

        The UPDATE is conditional on the row still being "queued" at write
        time, not just at some earlier read: a cancel landing between a
        caller's own status check and this call must win, not be silently
        overwritten by an unconditional UPDATE. Returns whether this call
        actually won the claim — a caller that gets False does not own the
        job and must not proceed as if it does.
        """
        def _write(c: Any) -> bool:
            cur = c.execute(
                "UPDATE jobs SET status = 'running', pid = ?, started_at = ?"
                " WHERE id = ? AND status = 'queued'",
                (pid, time.time(), job_id),
            )
            return cur.rowcount > 0

        return execute_write(self._conn, _write)

    def set_units_total(self, job_id: str, units_total: int, *, pid: int) -> bool:
        """Resize the work of the job this worker owns.

        A job is enqueued with as much as its caller knew about, which can be
        less than there turns out to be: an OCR job's page list is a floor the
        conversion path stopped counting at, and the worker settles the real
        extent itself before it starts. Progress has to be reported against
        what will actually be done, not against that floor.

        Conditional on the row still being this worker's running job, for the
        same reason :meth:`finish` is: a cancel can land while the worker is
        still working out how much there is to do, and ``reconcile``'s age
        fallback can leave two workers holding one job. Returns whether this
        call actually wrote.
        """
        def _write(c: Any) -> bool:
            cur = c.execute(
                "UPDATE jobs SET units_total = ?"
                " WHERE id = ? AND status = 'running' AND pid = ?",
                (units_total, job_id, pid),
            )
            return cur.rowcount > 0

        return execute_write(self._conn, _write)

    def record_unit(self, job_id: str, unit: int, text: str) -> None:
        def _write(c: Any) -> None:
            c.execute(
                "INSERT INTO job_units (job_id, unit, text) VALUES (?, ?, ?)"
                " ON CONFLICT(job_id, unit) DO UPDATE SET text = excluded.text",
                (job_id, unit, text),
            )
            # Recount rather than increment: re-writing a unit must not inflate
            # progress, and a resumed worker may redo a unit it already wrote.
            c.execute(
                "UPDATE jobs SET units_done ="
                " (SELECT COUNT(*) FROM job_units WHERE job_id = ?) WHERE id = ?",
                (job_id, job_id),
            )

        execute_write(self._conn, _write)

    def units(self, job_id: str) -> list[tuple[int, str]]:
        rows = self._conn.execute(
            "SELECT unit, text FROM job_units WHERE job_id = ? ORDER BY unit",
            (job_id,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def done_units(self, job_id: str) -> set[int]:
        rows = self._conn.execute(
            "SELECT unit FROM job_units WHERE job_id = ?", (job_id,)
        ).fetchall()
        return {r[0] for r in rows}

    def finish(self, job_id: str, *, pid: int, error: str | None = None) -> bool:
        """Record the outcome of the job this worker owns.

        Conditional on the row still being this worker's running job, for the
        same reason :meth:`claim` is conditional. Two ways an unconditional
        UPDATE gets it wrong. A cancel can land after the worker's last
        per-page check — everything after that check (the sidecar, chunking,
        indexing, embedding) is minutes for a book — and would be overwritten,
        ``ended_at`` included. And ``reconcile``'s age fallback can requeue a
        job whose worker is genuinely alive, so two workers can hold the same
        job: the pid clause keeps the one that no longer owns the row from
        flipping the outcome of the one that does.

        Returns whether this call actually wrote the outcome. A caller that
        gets False did not finish the job and must not report that it did.
        """
        def _write(c: Any) -> bool:
            cur = c.execute(
                "UPDATE jobs SET status = ?, ended_at = ?, error = ?, pid = NULL"
                " WHERE id = ? AND status = 'running' AND pid = ?",
                ("failed" if error else "done", time.time(), error, job_id, pid),
            )
            return cur.rowcount > 0

        return execute_write(self._conn, _write)

    def cancel(self, job_id: str) -> None:
        execute_write(
            self._conn,
            lambda c: c.execute(
                "UPDATE jobs SET status = 'cancelled', ended_at = ?, pid = NULL"
                " WHERE id = ?",
                (time.time(), job_id),
            ),
        )

    def reconcile(
        self, *, alive: Callable[[int], bool],
        now: float | None = None, max_age_s: float = RECONCILE_AGE_S,
    ) -> list[Job]:
        """Requeue jobs marked running whose worker process is gone, or that
        have been running longer than *max_age_s* regardless of what their
        pid looks like.

        The age fallback exists because pid-liveness alone is not enough:
        after a host reboot, pid allocation restarts from a low number, so a
        stale "running" row's pid can end up matching a completely unrelated
        live process, and `alive` would report a job as fine that has no
        worker at all. Called at gateway start. Finished units are kept, so a
        requeued job resumes rather than restarting.
        """
        if now is None:
            now = time.time()
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE status = 'running'"
        ).fetchall()
        orphans = []
        for r in rows:
            pid, started_at = r[8], r[10]
            pid_dead = not (pid and alive(pid))
            too_old = started_at is not None and (now - started_at) > max_age_s
            if pid_dead or too_old:
                orphans.append(_row_to_job(r))
        for job in orphans:
            execute_write(
                self._conn,
                lambda c, jid=job.id: c.execute(
                    "UPDATE jobs SET status = 'queued', pid = NULL,"
                    " started_at = NULL WHERE id = ?",
                    (jid,),
                ),
            )
        return [j for j in (self.get(o.id) for o in orphans) if j is not None]
