"""Tests for internal git versioning of workflow definitions."""

from durin.workflow.version_store import WorkflowVersionStore


def _wfdir(tmp_path):
    d = tmp_path / "workflows"
    d.mkdir()
    return d


def test_snapshot_records_a_version_and_history_shows_it(tmp_path):
    d = _wfdir(tmp_path)
    (d / "a.json").write_text('{"name": "a", "start": "x", "nodes": []}')
    store = WorkflowVersionStore(d)
    sha = store.snapshot("run a")
    assert sha
    assert any(c.subject == "run a" for c in store.history())


def test_snapshot_is_a_noop_when_definitions_unchanged(tmp_path):
    d = _wfdir(tmp_path)
    (d / "a.json").write_text('{"name": "a"}')
    store = WorkflowVersionStore(d)
    assert store.snapshot("run 1")          # first snapshot records
    assert store.snapshot("run 2") is None  # nothing changed -> no new version


def test_editing_a_workflow_creates_a_new_version_in_its_history(tmp_path):
    d = _wfdir(tmp_path)
    f = d / "a.json"
    f.write_text('{"name": "a", "v": 1}')
    store = WorkflowVersionStore(d)
    store.snapshot("run 1")
    f.write_text('{"name": "a", "v": 2}')
    store.snapshot("run 2")
    subjects = [c.subject for c in store.history("a")]
    assert "run 1" in subjects and "run 2" in subjects   # both versions tracked for a.json


def test_history_scopes_out_commits_from_before_a_workflow_existed(tmp_path):
    d = _wfdir(tmp_path)
    (d / "a.json").write_text('{"name": "a"}')
    store = WorkflowVersionStore(d)
    store.snapshot("first a")
    (d / "b.json").write_text('{"name": "b"}')
    store.snapshot("add b")
    b_subjects = [c.subject for c in store.history("b")]
    assert "add b" in b_subjects
    assert "first a" not in b_subjects        # b.json did not exist at the 'first a' version


def test_snapshot_is_best_effort_on_missing_dir(tmp_path):
    store = WorkflowVersionStore(tmp_path / "does-not-exist")
    assert store.snapshot("x") is None        # no dir -> None, never raises
    assert store.history() == []
