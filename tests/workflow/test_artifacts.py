from pathlib import Path
from durin.workflow.artifacts import artifact_dir, prune_runs


def test_artifact_dir_is_keyed_by_run_node_iteration(tmp_path):
    d = artifact_dir(tmp_path, "run123", "make", 2)
    assert d == tmp_path / ".workflow" / "run123" / "make" / "2"
    assert d.is_dir()                                   # created


def test_artifact_root_gitignores_itself(tmp_path):
    artifact_dir(tmp_path, "r", "n", 1)
    assert (tmp_path / ".workflow" / ".gitignore").read_text() == "*\n"


def test_two_runs_do_not_collide(tmp_path):
    a = artifact_dir(tmp_path, "runA", "make", 1)
    b = artifact_dir(tmp_path, "runB", "make", 1)
    assert a != b and a.is_dir() and b.is_dir()


def test_prune_runs_keeps_last_n(tmp_path):
    root = tmp_path / ".workflow"
    for i in range(5):
        artifact_dir(tmp_path, f"run{i}", "n", 1)
    prune_runs(tmp_path, keep=2)
    remaining = {p.name for p in root.iterdir() if p.is_dir()}
    assert len(remaining) == 2                          # only the 2 newest run dirs survive


def _aged(tmp_path, run_id, age_s):
    """Create a run folder whose mtime is `age_s` seconds in the past."""
    import os
    import time

    artifact_dir(tmp_path, run_id, "n", 1)
    folder = tmp_path / ".workflow" / run_id
    stamp = time.time() - age_s
    os.utime(folder, (stamp, stamp))
    return folder


def test_prune_runs_never_deletes_a_protected_run(tmp_path):
    """A live run's folder must survive pruning even when it is the oldest on
    disk — a long node freezes the folder's mtime, and 20 newer runs starting
    during it would otherwise delete the working folder out from under it."""
    live = _aged(tmp_path, "live-run", age_s=3600)      # oldest by far
    for i in range(3):
        _aged(tmp_path, f"newer{i}", age_s=60 - i)

    prune_runs(tmp_path, keep=2, protect={"live-run"})

    assert live.is_dir()
    survivors = {p.name for p in (tmp_path / ".workflow").iterdir() if p.is_dir()}
    assert "live-run" in survivors


def test_protected_runs_do_not_consume_keep_slots(tmp_path):
    """Protection is on top of `keep`, not part of it: with keep=2, the two
    newest terminal folders survive alongside the protected one."""
    _aged(tmp_path, "live-run", age_s=3600)
    for i in range(4):
        _aged(tmp_path, f"term{i}", age_s=(400 - i * 10))  # term3 newest ... term0 oldest

    prune_runs(tmp_path, keep=2, protect={"live-run"})

    survivors = {p.name for p in (tmp_path / ".workflow").iterdir() if p.is_dir()}
    assert survivors == {"live-run", "term3", "term2"}


def test_prune_runs_without_protect_behaves_as_before(tmp_path):
    for i in range(4):
        _aged(tmp_path, f"r{i}", age_s=(400 - i * 10))
    prune_runs(tmp_path, keep=3)
    survivors = {p.name for p in (tmp_path / ".workflow").iterdir() if p.is_dir()}
    assert survivors == {"r3", "r2", "r1"}
