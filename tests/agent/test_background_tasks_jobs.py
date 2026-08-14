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


def test_a_queued_job_reads_as_queued_in_the_tray(tmp_path, registry):
    """Not "running". Under the OCR concurrency cap, queued is the normal
    state of every book but the first in a multi-document ingest, and calling
    it running gives it a live clock counting time nothing is working on it."""
    registry.enqueue(kind="ocr", label="a", payload={}, session_key="s", units_total=1)
    assert collect_tasks(tmp_path, session_key="s", jobs=registry)[0]["status"] == "queued"


def test_a_claimed_job_reads_as_running(tmp_path, registry):
    """The other side of the same distinction: once a worker holds the row,
    the tray says running and means it."""
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key="s", units_total=1)
    registry.claim(job.id, pid=4242)
    assert collect_tasks(tmp_path, session_key="s", jobs=registry)[0]["status"] == "running"


def test_only_jobs_ever_report_queued(tmp_path, registry):
    """Sub-agents and workflow runs have no queued state to report -- their
    mappers cannot produce the word, so a consumer that handles it only needs
    to handle it for jobs."""
    from durin.agent.background_tasks import _subagent_status, _workflow_status

    phases = ["initializing", "awaiting_tools", "tools_completed", "final_response",
              "done", "error", "cancelled"]
    run_statuses = ["running", "completed", "needs_input", "exhausted", "aborted",
                    "cancelled", "crashed"]
    assert "queued" not in {_subagent_status(p) for p in phases}
    assert "queued" not in {_workflow_status(s) for s in run_statuses}


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


def test_a_failed_job_carries_the_reason_it_failed(tmp_path, registry):
    """The worker records a usable reason ("page 7: ...", "sidecar write: ...",
    "library index: ..."). Dropping it here is what makes a failed job
    unexplainable everywhere downstream: this row is all the tray and the
    tasks tool ever see."""
    job = registry.enqueue(kind="ocr", label="a", payload={}, session_key="s", units_total=1)
    registry.claim(job.id, pid=4242)
    registry.finish(job.id, pid=4242, error="page 7: OSError: disk gone")

    row = collect_tasks(tmp_path, session_key="s", jobs=registry)[0]

    assert row["error"] == "page 7: OSError: disk gone"


def test_a_job_that_did_not_fail_has_no_error(tmp_path, registry):
    registry.enqueue(kind="ocr", label="a", payload={}, session_key="s", units_total=1)
    assert collect_tasks(tmp_path, session_key="s", jobs=registry)[0]["error"] is None


def test_jobs_from_another_session_are_not_listed(tmp_path, registry):
    registry.enqueue(kind="ocr", label="theirs", payload={}, session_key="other", units_total=1)
    assert collect_tasks(tmp_path, session_key="mine", jobs=registry) == []


def test_no_registry_means_jobs_contribute_nothing(tmp_path):
    # Same contract the other two sources already have.
    assert collect_tasks(tmp_path, session_key="s", jobs=None) == []
