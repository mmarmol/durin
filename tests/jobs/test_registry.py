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
    registry.finish(job.id)
    reread = registry.get(job.id)
    assert reread.status == "done"
    assert reread.ended_at is not None
    assert reread.error is None


def test_finish_with_error_is_failed(registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key=None, units_total=1)
    registry.finish(job.id, error="engine exploded")
    reread = registry.get(job.id)
    assert reread.status == "failed"
    assert reread.error == "engine exploded"


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
    registry.finish(job.id)
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
