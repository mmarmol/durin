"""The long-job registry: enqueue, progress, resumption, reconciliation."""

import pytest

from durin.jobs.registry import RECONCILE_AGE_S, Job, JobRegistry


@pytest.fixture()
def registry(tmp_path):
    return JobRegistry(tmp_path / "jobs.db")


def test_enqueue_returns_a_queued_job(registry):
    job = registry.enqueue(
        kind="ocr", label="book.pdf", payload={"path": "/tmp/book.pdf"},
        session_key="chat:1", units_total=412,
    )
    assert isinstance(job, Job)
    assert job.status == "queued"
    assert job.units_total == 412
    assert job.units_done == 0
    assert job.payload == {"path": "/tmp/book.pdf"}


def test_get_reads_the_job_back(registry):
    job = registry.enqueue(kind="ocr", label="a.pdf", payload={}, session_key=None, units_total=3)
    assert registry.get(job.id).label == "a.pdf"


def test_get_of_an_unknown_id_is_none(registry):
    assert registry.get("nope") is None


def test_list_for_session_is_newest_first(registry):
    first = registry.enqueue(kind="ocr", label="one", payload={}, session_key="s", units_total=1)
    second = registry.enqueue(kind="ocr", label="two", payload={}, session_key="s", units_total=1)
    ids = [j.id for j in registry.list_for_session("s")]
    assert ids == [second.id, first.id]


def test_list_for_session_breaks_ties_on_identical_created_at(registry, monkeypatch):
    # Two jobs enqueued in the same clock tick get the same `created_at`.
    # `ORDER BY created_at DESC` alone leaves that tie in implementation-defined
    # order; freeze the clock so the tie is real (not a timing-margin accident)
    # and prove the rowid tiebreaker still puts the later insert first.
    monkeypatch.setattr("durin.jobs.registry.time.time", lambda: 1_700_000_000.0)
    first = registry.enqueue(kind="ocr", label="one", payload={}, session_key="s", units_total=1)
    second = registry.enqueue(kind="ocr", label="two", payload={}, session_key="s", units_total=1)
    assert first.created_at == second.created_at  # confirm the tie actually happened

    ids = [j.id for j in registry.list_for_session("s")]
    assert ids == [second.id, first.id]


def test_list_for_session_excludes_other_sessions(registry):
    registry.enqueue(kind="ocr", label="mine", payload={}, session_key="a", units_total=1)
    registry.enqueue(kind="ocr", label="theirs", payload={}, session_key="b", units_total=1)
    assert [j.label for j in registry.list_for_session("a")] == ["mine"]


def test_claim_marks_running_with_a_pid(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=2)
    won = registry.claim(job.id, pid=4242)
    assert won is True
    reread = registry.get(job.id)
    assert reread.status == "running"
    assert reread.pid == 4242
    assert reread.started_at is not None


def test_claim_loses_to_a_cancel_that_landed_first(registry):
    # The exact interleaving that matters: a worker reads "queued", then a
    # cancel lands, then the worker's claim runs. The claim must not win —
    # an unconditional UPDATE would silently flip a cancelled job back to
    # "running" and the worker would transcribe the whole document anyway.
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=5)
    registry.cancel(job.id)

    won = registry.claim(job.id, pid=4242)

    assert won is False
    reread = registry.get(job.id)
    assert reread.status == "cancelled"  # not overwritten back to "running"
    assert reread.pid is None


def test_claim_loses_when_another_worker_already_claimed_it(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=5)
    assert registry.claim(job.id, pid=1111) is True

    won_again = registry.claim(job.id, pid=2222)

    assert won_again is False
    reread = registry.get(job.id)
    assert reread.pid == 1111  # the second claim did not steal ownership


def test_claim_with_a_kind_cap_admits_one_and_refuses_the_next(tmp_path):
    # Two independent connections onto the same file, not two threads: the
    # two claim() calls below are sequential Python calls, so this cannot by
    # itself exercise a genuine concurrent race. What it does prove is
    # cross-connection visibility -- reg_b's claim() runs its COUNT query
    # after reg_a's write has already committed, on a *different* sqlite3
    # connection, and correctly sees it; a caching or stale-read bug specific
    # to a second connection could break that in a way no single-connection
    # test would ever catch. The guarantee that two truly concurrent claims
    # under the same cap serialize correctly instead of double-admitting is
    # BEGIN IMMEDIATE's own design contract (one writer at a time for the
    # whole database, see sqlite_util.execute_write) -- a property of SQLite,
    # not something this sequential test demonstrates on its own.
    path = tmp_path / "jobs.db"
    reg_a = JobRegistry(path)
    reg_b = JobRegistry(path)
    job_a = reg_a.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    job_b = reg_a.enqueue(kind="ocr", label="b", payload={}, session_key=None, units_total=1)

    won_a = reg_a.claim(job_a.id, pid=111, kind_cap=("ocr", 1))
    won_b = reg_b.claim(job_b.id, pid=222, kind_cap=("ocr", 1))

    assert (won_a, won_b) == (True, False)


def test_claim_refused_by_the_cap_leaves_no_trace(registry):
    # A cap refusal must read identically to "never touched" -- the whole
    # point is that the row is exactly as fit for a later claim (the chain,
    # or a startup pickup) as it was before this call.
    running = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    registry.claim(running.id, pid=111)  # occupies the one cap slot
    job = registry.enqueue(kind="ocr", label="b", payload={}, session_key=None, units_total=1)

    won = registry.claim(job.id, pid=222, kind_cap=("ocr", 1))

    assert won is False
    reread = registry.get(job.id)
    assert reread.status == "queued"
    assert reread.pid is None
    assert reread.started_at is None


def test_claim_kind_cap_only_counts_the_named_kind(registry):
    # A running job of a different kind must not count against this kind's
    # cap -- proves the subquery is scoped by `kind`, not just `status`.
    other = registry.enqueue(kind="reindex", label="a", payload={}, session_key=None, units_total=1)
    registry.claim(other.id, pid=111)
    job = registry.enqueue(kind="ocr", label="b", payload={}, session_key=None, units_total=1)

    won = registry.claim(job.id, pid=222, kind_cap=("ocr", 1))

    assert won is True
    assert registry.get(job.id).status == "running"


def test_claim_kind_cap_admits_up_to_the_limit(registry):
    # Cap of 2 with one already running still has a slot for a second.
    first = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    registry.claim(first.id, pid=111)
    second = registry.enqueue(kind="ocr", label="b", payload={}, session_key=None, units_total=1)

    won = registry.claim(second.id, pid=222, kind_cap=("ocr", 2))

    assert won is True
    assert registry.get(second.id).status == "running"


def test_claim_without_a_kind_cap_ignores_other_running_jobs_of_the_kind(registry):
    # The cap is opt-in: a caller that does not pass kind_cap gets the
    # pre-cap behaviour exactly, however many jobs of that kind are running.
    first = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    registry.claim(first.id, pid=111)
    second = registry.enqueue(kind="ocr", label="b", payload={}, session_key=None, units_total=1)

    assert registry.claim(second.id, pid=222) is True


def test_next_queued_returns_the_oldest_queued_job_of_the_kind(registry):
    first = registry.enqueue(kind="ocr", label="one", payload={}, session_key=None, units_total=1)
    registry.enqueue(kind="ocr", label="two", payload={}, session_key=None, units_total=1)

    assert registry.next_queued("ocr").id == first.id


def test_next_queued_breaks_ties_on_rowid_oldest_first(registry, monkeypatch):
    # Mirrors test_list_for_session_breaks_ties_on_identical_created_at, but
    # next_queued wants the OLDEST (lowest rowid) on a tie, not the newest --
    # the opposite direction from list_for_session's DESC ordering.
    monkeypatch.setattr("durin.jobs.registry.time.time", lambda: 1_700_000_000.0)
    first = registry.enqueue(kind="ocr", label="one", payload={}, session_key=None, units_total=1)
    second = registry.enqueue(kind="ocr", label="two", payload={}, session_key=None, units_total=1)
    assert first.created_at == second.created_at  # confirm the tie actually happened

    assert registry.next_queued("ocr").id == first.id


def test_next_queued_ignores_other_kinds(registry):
    registry.enqueue(kind="reindex", label="a", payload={}, session_key=None, units_total=1)
    ocr_job = registry.enqueue(kind="ocr", label="b", payload={}, session_key=None, units_total=1)

    assert registry.next_queued("ocr").id == ocr_job.id


def test_next_queued_ignores_non_queued_jobs(registry):
    claimed = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    registry.claim(claimed.id, pid=111)  # now "running", not "queued"
    queued = registry.enqueue(kind="ocr", label="b", payload={}, session_key=None, units_total=1)

    assert registry.next_queued("ocr").id == queued.id


def test_next_queued_returns_none_when_nothing_queued(registry):
    assert registry.next_queued("ocr") is None


def test_queued_jobs_returns_up_to_limit_oldest_first(registry):
    first = registry.enqueue(kind="ocr", label="one", payload={}, session_key=None, units_total=1)
    second = registry.enqueue(kind="ocr", label="two", payload={}, session_key=None, units_total=1)
    registry.enqueue(kind="ocr", label="three", payload={}, session_key=None, units_total=1)

    ids = [j.id for j in registry.queued_jobs("ocr", 2)]

    assert ids == [first.id, second.id]


def test_queued_jobs_returns_fewer_than_limit_when_that_is_all_there_is(registry):
    only = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    assert [j.id for j in registry.queued_jobs("ocr", 5)] == [only.id]


def test_queued_jobs_returns_empty_when_nothing_queued(registry):
    assert registry.queued_jobs("ocr", 3) == []


def test_queued_jobs_ignores_other_kinds_and_non_queued_statuses(registry):
    registry.enqueue(kind="reindex", label="a", payload={}, session_key=None, units_total=1)
    claimed = registry.enqueue(kind="ocr", label="b", payload={}, session_key=None, units_total=1)
    registry.claim(claimed.id, pid=111)  # now "running", not "queued"
    queued = registry.enqueue(kind="ocr", label="c", payload={}, session_key=None, units_total=1)

    assert [j.id for j in registry.queued_jobs("ocr", 5)] == [queued.id]


def test_set_units_total_resizes_the_job_this_worker_owns(registry):
    # A worker that finds more work than its payload named says so, so the
    # tray's denominator is the work that will actually be done.
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=6)
    registry.claim(job.id, pid=4242)

    assert registry.set_units_total(job.id, 40, pid=4242) is True
    assert registry.get(job.id).units_total == 40


def test_set_units_total_is_refused_for_a_job_this_worker_no_longer_owns(registry):
    # Guarded like finish(), and for the same reason: a cancel can land while
    # the worker is still deciding how much work there is, and reconcile's age
    # fallback can leave two workers holding one job. Neither may write over
    # the row it does not own.
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=6)
    registry.claim(job.id, pid=4242)
    registry.cancel(job.id)

    assert registry.set_units_total(job.id, 40, pid=4242) is False
    assert registry.get(job.id).units_total == 6

    registry.claim(job.id, pid=4242)  # cancelled, so this cannot win either
    assert registry.set_units_total(job.id, 40, pid=9999) is False
    assert registry.get(job.id).units_total == 6


def test_record_unit_advances_progress(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=3)
    registry.record_unit(job.id, 1, "page one text")
    registry.record_unit(job.id, 2, "page two text")
    assert registry.get(job.id).units_done == 2


def test_record_unit_is_idempotent(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=3)
    registry.record_unit(job.id, 1, "first write")
    registry.record_unit(job.id, 1, "second write")
    assert registry.get(job.id).units_done == 1
    assert registry.units(job.id) == [(1, "second write")]


def test_units_come_back_in_page_order(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=3)
    registry.record_unit(job.id, 3, "third")
    registry.record_unit(job.id, 1, "first")
    registry.record_unit(job.id, 2, "second")
    assert [t for _, t in registry.units(job.id)] == ["first", "second", "third"]


def test_done_units_supports_resumption(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=5)
    registry.record_unit(job.id, 1, "a")
    registry.record_unit(job.id, 2, "b")
    assert registry.done_units(job.id) == {1, 2}


def test_finish_without_error_is_done(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    registry.claim(job.id, pid=4242)
    assert registry.finish(job.id, pid=4242) is True
    reread = registry.get(job.id)
    assert reread.status == "done"
    assert reread.ended_at is not None
    assert reread.error is None


def test_finish_with_error_is_failed(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    registry.claim(job.id, pid=4242)
    registry.finish(job.id, pid=4242, error="engine exploded")
    reread = registry.get(job.id)
    assert reread.status == "failed"
    assert reread.error == "engine exploded"


def test_finish_does_not_erase_a_cancel(registry):
    """A cancel can land after the worker's last per-page check: the sidecar
    write, the chunking, the FTS index and the embeddings all happen after it,
    and for a book that is minutes. An unconditional UPDATE would overwrite
    the cancellation -- ended_at included -- with a cheerful "done"."""
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    registry.claim(job.id, pid=4242)
    registry.cancel(job.id)
    cancelled_at = registry.get(job.id).ended_at

    assert registry.finish(job.id, pid=4242) is False

    reread = registry.get(job.id)
    assert reread.status == "cancelled"
    assert reread.ended_at == cancelled_at


def test_finish_belongs_to_the_worker_that_owns_the_row(registry):
    """reconcile's 6h age fallback can requeue a job whose worker is genuinely
    alive, and a second worker then claims it. The first one arriving late
    must not flip a row it no longer owns."""
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    registry.claim(job.id, pid=111)
    registry.reconcile(alive=lambda pid: False)
    registry.claim(job.id, pid=222)

    assert registry.finish(job.id, pid=111, error="boom") is False

    reread = registry.get(job.id)
    assert reread.status == "running"
    assert reread.pid == 222
    assert reread.error is None


def test_cancel_marks_cancelled(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    registry.cancel(job.id)
    assert registry.get(job.id).status == "cancelled"


def test_reconcile_requeues_a_job_whose_worker_died(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=5)
    registry.claim(job.id, pid=999999)
    registry.record_unit(job.id, 1, "done before the crash")

    requeued = registry.reconcile(alive=lambda pid: False)

    assert [j.id for j in requeued] == [job.id]
    reread = registry.get(job.id)
    assert reread.status == "queued"
    # Work already done is kept: resumption is the point.
    assert reread.units_done == 1
    assert registry.done_units(job.id) == {1}


def test_reconcile_leaves_a_live_worker_alone(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=5)
    registry.claim(job.id, pid=1234)
    assert registry.reconcile(alive=lambda pid: True) == []
    assert registry.get(job.id).status == "running"


def test_reconcile_requeues_a_live_seeming_pid_once_it_is_too_old(registry):
    # After a host reboot, pid allocation restarts low, so a stale "running"
    # row's pid can coincidentally match a completely unrelated live process.
    # `alive` alone cannot see that — the age fallback is what catches it.
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=5)
    registry.claim(job.id, pid=1234)
    claimed_at = registry.get(job.id).started_at

    requeued = registry.reconcile(
        alive=lambda pid: True, now=claimed_at + RECONCILE_AGE_S + 1, max_age_s=RECONCILE_AGE_S)

    assert [j.id for j in requeued] == [job.id]
    assert registry.get(job.id).status == "queued"


def test_reconcile_leaves_a_live_pid_alone_when_still_within_the_age_window(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=5)
    registry.claim(job.id, pid=1234)
    claimed_at = registry.get(job.id).started_at

    requeued = registry.reconcile(
        alive=lambda pid: True, now=claimed_at + RECONCILE_AGE_S - 1, max_age_s=RECONCILE_AGE_S)

    assert requeued == []
    assert registry.get(job.id).status == "running"


def test_reconcile_ignores_finished_jobs(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    registry.claim(job.id, pid=1)
    registry.finish(job.id, pid=1)
    assert registry.reconcile(alive=lambda pid: False) == []


def test_a_second_registry_over_the_same_file_sees_the_same_jobs(tmp_path):
    # The worker is a separate process opening the same database.
    path = tmp_path / "jobs.db"
    job = JobRegistry(path).enqueue(
        kind="ocr", label="a", payload={"k": "v"}, session_key=None, units_total=2
    )
    assert JobRegistry(path).get(job.id).payload == {"k": "v"}


def test_kind_is_not_restricted_to_ocr(registry):
    # The registry is generic; OCR is only its first client.
    job = registry.enqueue(kind="reindex", label="rebuild", payload={}, session_key=None, units_total=7)
    assert registry.get(job.id).kind == "reindex"
