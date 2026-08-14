"""Long jobs appear in the same tray as sub-agents and workflow runs."""

import pytest

from durin.agent.background_tasks import collect_tasks
from durin.jobs.registry import JobRegistry


@pytest.fixture()
def registry(tmp_path):
    return JobRegistry(tmp_path / "jobs.db")


def test_a_job_appears_with_kind_job(tmp_path, registry):
    registry.enqueue(
        kind="ocr", label="book.pdf", payload={}, session_key="chat:1", units_total=412
    )
    tasks = collect_tasks(tmp_path, session_key="chat:1", jobs=registry)
    assert [t["kind"] for t in tasks] == ["job"]
    assert tasks[0]["label"] == "book.pdf"


def test_a_job_carries_its_page_progress(tmp_path, registry):
    job = registry.enqueue(
        kind="ocr", label="book.pdf", payload={}, session_key="chat:1", units_total=3
    )
    registry.record_unit(job.id, 1, "one")
    tasks = collect_tasks(tmp_path, session_key="chat:1", jobs=registry)
    assert tasks[0]["units_total"] == 3
    assert tasks[0]["units_done"] == 1


def test_a_queued_job_reads_as_running_in_the_tray(tmp_path, registry):
    # The tray's vocabulary has no "queued"; from the user's side the work is
    # accepted and pending, which is what "running" communicates.
    registry.enqueue(kind="ocr", label="a", payload={}, session_key="s", units_total=1)
    assert collect_tasks(tmp_path, session_key="s", jobs=registry)[0]["status"] == "running"


def test_a_finished_job_reads_as_done(tmp_path, registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key="s", units_total=1)
    registry.claim(job.id, pid=4242)
    registry.finish(job.id, pid=4242)
    assert collect_tasks(tmp_path, session_key="s", jobs=registry)[0]["status"] == "done"


def test_a_failed_job_reads_as_failed(tmp_path, registry):
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key="s", units_total=1)
    registry.claim(job.id, pid=4242)
    registry.finish(job.id, pid=4242, error="boom")
    assert collect_tasks(tmp_path, session_key="s", jobs=registry)[0]["status"] == "failed"


def test_jobs_from_another_session_are_not_listed(tmp_path, registry):
    registry.enqueue(kind="ocr", label="theirs", payload={}, session_key="other", units_total=1)
    assert collect_tasks(tmp_path, session_key="mine", jobs=registry) == []


def test_no_registry_means_jobs_contribute_nothing(tmp_path):
    # Same contract the other two sources already have.
    assert collect_tasks(tmp_path, session_key="s", jobs=None) == []
