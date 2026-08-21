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
# prune_runs's RUN-COUNT retention (`keep`) never applies to .workflow/keys/ —
# it holds stable, caller-keyed working folders, not per-run folders, and has
# no run_id to protect. It still ages out on its OWN clock (see the
# age-based retention section below): a fresh keyed dir survives any `keep`,
# but an idle one past KEYED_WORK_MAX_AGE_DAYS is reaped regardless of `keep`.
# ---------------------------------------------------------------------------


def test_prune_runs_never_touches_the_keys_directory(tmp_path):
    keyed_work_dir(tmp_path, "w", "k")
    for i in range(4):
        _aged(tmp_path, f"r{i}", age_s=(400 - i * 10))

    prune_runs(tmp_path, keep=1)   # keep=1: every plain run dir but the newest is fair game

    survivors = {p.name for p in (tmp_path / ".workflow").iterdir() if p.is_dir()}
    assert "keys" in survivors
    assert (tmp_path / ".workflow" / "keys" / "w-50e721e4" / "k-8254c329" / "work").is_dir()


# ---------------------------------------------------------------------------
# Age-based retention for keyed dirs: a work_key folder has no run_id and so
# never ages out via `keep` — its only bound is elapsed idle time
# (KEYED_WORK_MAX_AGE_DAYS). A dir whose run lock is currently HELD is never
# removed, no matter how old its content looks.
# ---------------------------------------------------------------------------


def _age_keyed_dir(tmp_path, workflow, key, age_s):
    """Create a keyed work dir with one file in it, then backdate every path
    under the key dir (the file, the work/ folder, and the key dir itself) to
    `age_s` seconds in the past — so its newest-content-mtime is unambiguously
    old, regardless of which of those paths the retention sweep consults."""
    import os
    import time

    work_dir = keyed_work_dir(tmp_path, workflow, key)
    report = work_dir / "report.md"
    report.write_text("done")
    stamp = time.time() - age_s
    for p in (report, work_dir, work_dir.parent):
        os.utime(p, (stamp, stamp))
    return work_dir


def test_prune_runs_removes_a_stale_keyed_dir(tmp_path):
    from durin.workflow.artifacts import KEYED_WORK_MAX_AGE_DAYS

    work_dir = _age_keyed_dir(tmp_path, "w", "k", age_s=(KEYED_WORK_MAX_AGE_DAYS + 1) * 86400)
    key_dir = work_dir.parent

    prune_runs(tmp_path, keep=1)

    assert not key_dir.exists()


def test_prune_runs_keeps_a_fresh_keyed_dir(tmp_path):
    work_dir = keyed_work_dir(tmp_path, "w", "k")
    (work_dir / "report.md").write_text("done")

    prune_runs(tmp_path, keep=1)

    assert work_dir.is_dir()
    assert (work_dir / "report.md").read_text() == "done"


def test_prune_runs_never_removes_a_keyed_dir_whose_lock_is_held(tmp_path):
    """A stale-by-mtime keyed dir whose run lock is currently held by another
    thread/process must survive — a live (if unusually slow) run must never
    lose its working folder mid-flight. Holds the lock on a BACKGROUND
    thread (not this one) so the check exercises a real OS-level flock
    rather than this module's own reentrant same-thread tracking."""
    import threading

    from durin.utils.file_lock import cross_process_lock
    from durin.workflow.artifacts import KEYED_WORK_MAX_AGE_DAYS, keyed_run_lock_target

    work_dir = _age_keyed_dir(tmp_path, "w", "k", age_s=(KEYED_WORK_MAX_AGE_DAYS + 1) * 86400)
    key_dir = work_dir.parent

    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with cross_process_lock(keyed_run_lock_target(work_dir)):
            holding.set()
            assert release.wait(timeout=10), "test's own release signal was never sent"

    t = threading.Thread(target=_hold)
    t.start()
    try:
        assert holding.wait(timeout=10), "background thread never acquired the lock"
        prune_runs(tmp_path, keep=1)
    finally:
        release.set()
        t.join(timeout=10)

    assert key_dir.is_dir()          # protected — its lock was held throughout


def test_prune_runs_spares_a_keyed_dir_whose_work_key_is_still_live(tmp_path):
    """A dir named in `protect_keyed` (raw, pre-safe_key (workflow, work_key)
    pairs — what the caller reads from run_log.live_work_keys) must survive
    regardless of age or lock state. This is the backstop for a PARKED
    (needs_input) run: its cross-process lock already released the moment it
    parked, so the lock check alone cannot protect it — see
    test_work_key.py's engine-level proof of the actual parked-run scenario."""
    from durin.workflow.artifacts import KEYED_WORK_MAX_AGE_DAYS

    work_dir = _age_keyed_dir(tmp_path, "My Workflow", "Ticket #1",
                              age_s=(KEYED_WORK_MAX_AGE_DAYS + 1) * 86400)
    key_dir = work_dir.parent

    prune_runs(tmp_path, keep=1, protect_keyed={("My Workflow", "Ticket #1")})

    assert key_dir.is_dir()


def test_prune_runs_protect_keyed_does_not_spare_an_unrelated_key(tmp_path):
    """A `protect_keyed` entry for a DIFFERENT (workflow, work_key) pair must
    not accidentally spare an unrelated stale dir — the guard is a precise
    match, not a blanket "something is live so nothing is pruned"."""
    from durin.workflow.artifacts import KEYED_WORK_MAX_AGE_DAYS

    work_dir = _age_keyed_dir(tmp_path, "w", "k", age_s=(KEYED_WORK_MAX_AGE_DAYS + 1) * 86400)
    key_dir = work_dir.parent

    prune_runs(tmp_path, keep=1, protect_keyed={("other-workflow", "other-key")})

    assert not key_dir.exists()


# ---------------------------------------------------------------------------
# Debounce: the keyed sweep's recursive mtime walk touches every file under
# every keys/<workflow>/<key>/ dir, so it must not run on EVERY prune_runs
# call (i.e. every workflow run start) — only at most once per
# KEYED_SWEEP_MIN_INTERVAL_S, tracked via an on-disk marker (keys/.last-sweep)
# since prune_runs is called fresh, sometimes from a different process, with
# nothing long-lived enough to hold an in-memory timestamp.
# ---------------------------------------------------------------------------


def test_second_prune_within_the_interval_skips_the_keyed_walk(tmp_path):
    from durin.workflow.artifacts import KEYED_WORK_MAX_AGE_DAYS

    # First call: no marker yet, so the sweep runs and reaps this stale dir.
    work_dir_1 = _age_keyed_dir(tmp_path, "w", "k1", age_s=(KEYED_WORK_MAX_AGE_DAYS + 1) * 86400)
    key_dir_1 = work_dir_1.parent
    prune_runs(tmp_path, keep=1)
    assert not key_dir_1.exists()

    marker = tmp_path / ".workflow" / "keys" / ".last-sweep"
    assert marker.is_file()
    first_sweep_mtime = marker.stat().st_mtime

    # A SECOND stale dir appears immediately after — well within the debounce
    # window. The second prune_runs call must skip the walk entirely: the new
    # dir survives (it was never even inspected) and the marker is untouched.
    work_dir_2 = _age_keyed_dir(tmp_path, "w", "k2", age_s=(KEYED_WORK_MAX_AGE_DAYS + 1) * 86400)
    key_dir_2 = work_dir_2.parent
    prune_runs(tmp_path, keep=1)

    assert key_dir_2.is_dir(), "the second sweep ran despite being within the debounce interval"
    assert marker.stat().st_mtime == first_sweep_mtime, "the marker was re-touched by a debounced call"


def test_prune_runs_after_the_interval_elapses_sweeps_again(tmp_path):
    import os
    import time

    from durin.workflow.artifacts import KEYED_SWEEP_MIN_INTERVAL_S, KEYED_WORK_MAX_AGE_DAYS

    # A fresh keyed dir just so keys/ exists — otherwise the very first sweep
    # short-circuits on "keys/ doesn't exist yet" and never creates the marker.
    keyed_work_dir(tmp_path, "seed", "seed")
    prune_runs(tmp_path, keep=1)
    marker = tmp_path / ".workflow" / "keys" / ".last-sweep"
    assert marker.is_file()
    stale_marker = time.time() - (KEYED_SWEEP_MIN_INTERVAL_S + 1)
    os.utime(marker, (stale_marker, stale_marker))   # simulate the interval having elapsed

    work_dir = _age_keyed_dir(tmp_path, "w", "k", age_s=(KEYED_WORK_MAX_AGE_DAYS + 1) * 86400)
    key_dir = work_dir.parent
    prune_runs(tmp_path, keep=1)

    assert not key_dir.exists(), "the sweep should have run again once the interval elapsed"
