from pathlib import Path

import pytest

from durin.workflow.artifacts import artifact_dir, keyed_work_dir, prune_runs, safe_key


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


# ---------------------------------------------------------------------------
# safe_key: sanitizing a caller-supplied workflow_name/work_key into a path
# segment. The result is ALWAYS <sanitized-prefix, capped at 60>-<sha256(the
# ORIGINAL, un-sanitized value)[:8]> — collision-proof by construction (two
# distinct raw values that happen to sanitize to the same prefix, e.g.
# "Ticket #1" and "ticket_#1", still get different hash suffixes and
# therefore different directories) while staying human-readable.
# ---------------------------------------------------------------------------


def test_safe_key_lowercases_and_replaces_unsafe_chars():
    # space AND '#' each -> their own '_'; suffix = sha256("Ticket #23124")[:8]
    assert safe_key("Ticket #23124") == "ticket__23124-7e5e630c"


def test_safe_key_keeps_dots_underscores_hyphens():
    assert safe_key("a.b_c-D") == "a.b_c-d-bd8597a0"


def test_safe_key_rejects_path_separators():
    with pytest.raises(ValueError):
        safe_key("../evil")


def test_safe_key_rejects_empty_string():
    with pytest.raises(ValueError):
        safe_key("")


def test_safe_key_rejects_dots_only():
    with pytest.raises(ValueError):
        safe_key("...")


def test_safe_key_strips_leading_dots():
    assert safe_key(".hidden") == "hidden-16924190"


def test_safe_key_caps_prefix_at_60_chars_then_appends_the_hash_suffix():
    result = safe_key("x" * 200)
    assert result == "x" * 60 + "-aa20c23e"


def test_safe_key_distinguishes_aliasing_keys_that_sanitize_identically():
    """The exact case the collision-proofing exists for: two DIFFERENT raw
    keys ("Ticket #1" has a space+hash; "ticket_#1" already has an
    underscore) sanitize to the identical prefix "ticket__1" but must not
    collapse onto the same directory."""
    a = safe_key("Ticket #1")
    b = safe_key("ticket_#1")
    assert a.startswith("ticket__1-") and b.startswith("ticket__1-")
    assert a != b


def test_safe_key_is_deterministic():
    assert safe_key("Ticket #23124") == safe_key("Ticket #23124")


# ---------------------------------------------------------------------------
# keyed_work_dir: the stable per-(workflow, key) folder under .workflow/keys/
# ---------------------------------------------------------------------------


def test_keyed_work_dir_shape(tmp_path):
    d = keyed_work_dir(tmp_path, "My Workflow", "Ticket #23124")
    assert d == (tmp_path / ".workflow" / "keys" / "my_workflow-d33387cb"
                / "ticket__23124-7e5e630c" / "work")
    assert d.is_dir()


def test_keyed_work_dir_stable_across_calls(tmp_path):
    a = keyed_work_dir(tmp_path, "w", "k")
    b = keyed_work_dir(tmp_path, "w", "k")
    assert a == b


def test_keyed_work_dir_rejects_invalid_key(tmp_path):
    with pytest.raises(ValueError):
        keyed_work_dir(tmp_path, "w", "")


# ---------------------------------------------------------------------------
# prune_runs must never reap .workflow/keys/ — it holds stable, caller-keyed
# working folders, not per-run folders, and has no run_id to protect.
# ---------------------------------------------------------------------------


def test_prune_runs_never_touches_the_keys_directory(tmp_path):
    keyed_work_dir(tmp_path, "w", "k")
    for i in range(4):
        _aged(tmp_path, f"r{i}", age_s=(400 - i * 10))

    prune_runs(tmp_path, keep=1)   # keep=1: every plain run dir but the newest is fair game

    survivors = {p.name for p in (tmp_path / ".workflow").iterdir() if p.is_dir()}
    assert "keys" in survivors
    assert (tmp_path / ".workflow" / "keys" / "w-50e721e4" / "k-8254c329" / "work").is_dir()
