"""Two resumes of one run cannot both start, whatever stopped the run."""
import pytest

from durin.workflow import run_log
from durin.workflow.resume_claim import ResumeClaim, ResumeLostError


def _record(tmp_path, status, **kw):
    run_log.start_run(tmp_path, "w", "r1", root_session_key=None, started_at=1.0)
    rec = {**run_log.read_manifest(tmp_path, "w", "r1"), "status": status, **kw}
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    return rec


@pytest.mark.parametrize("status,extra", [
    ("needs_input", {"needs_input_node": "ask"}), ("aborted", {"failed_node": "b"})])
def test_the_second_claim_of_a_record_loses(tmp_path, status, extra):
    rec = _record(tmp_path, status, **extra)
    ResumeClaim.take(tmp_path, "w", "r1", expected=rec)
    assert run_log.read_manifest(tmp_path, "w", "r1")["status"] == "running"
    with pytest.raises(ResumeLostError):
        ResumeClaim.take(tmp_path, "w", "r1", expected=rec)


def test_a_walk_that_never_wrote_the_record_gives_it_back(tmp_path):
    rec = _record(tmp_path, "aborted", failed_node="b")
    claim = ResumeClaim.take(tmp_path, "w", "r1", expected=rec)

    def refused():
        raise ValueError("rejected before the walk")

    with pytest.raises(ValueError):
        claim.guard(refused)()
    assert run_log.read_manifest(tmp_path, "w", "r1") == rec


def test_a_walk_that_wrote_the_record_keeps_it(tmp_path):
    rec = _record(tmp_path, "aborted", failed_node="b")
    claim = ResumeClaim.take(tmp_path, "w", "r1", expected=rec)

    def walked():
        run_log.start_run(tmp_path, "w", "r1", root_session_key=None, started_at=2.0, resumed=True)
        return "done"

    assert claim.guard(walked)() == "done"
    assert run_log.read_manifest(tmp_path, "w", "r1")["started_at"] == 2.0


def test_a_dropped_claim_never_walks(tmp_path):
    rec = _record(tmp_path, "needs_input", needs_input_node="ask")
    claim = ResumeClaim.take(tmp_path, "w", "r1", expected=rec)
    claim.drop()
    assert run_log.read_manifest(tmp_path, "w", "r1") == rec
    assert claim.guard(lambda: pytest.fail("walked after drop"))() is None


def test_a_claim_releases_once_and_never_undoes_a_later_claim(tmp_path):
    # A later claim by the same process on the same record writes exactly the
    # record the first claim wrote, so a second release of the first claim
    # cannot tell the two apart: it must not release at all.
    rec = _record(tmp_path, "aborted", failed_node="b")
    first = ResumeClaim.take(tmp_path, "w", "r1", expected=rec)
    first.drop()
    assert run_log.read_manifest(tmp_path, "w", "r1") == rec
    ResumeClaim.take(tmp_path, "w", "r1", expected=rec)
    first.drop()
    assert run_log.read_manifest(tmp_path, "w", "r1")["status"] == "running"


def test_a_guarded_walk_that_released_is_not_released_again_by_a_late_drop(tmp_path):
    rec = _record(tmp_path, "needs_input", needs_input_node="ask")
    first = ResumeClaim.take(tmp_path, "w", "r1", expected=rec)

    def refused():
        raise ValueError("rejected before the walk")

    with pytest.raises(ValueError):
        first.guard(refused)()
    ResumeClaim.take(tmp_path, "w", "r1", expected=rec)
    first.drop()
    assert run_log.read_manifest(tmp_path, "w", "r1")["status"] == "running"
