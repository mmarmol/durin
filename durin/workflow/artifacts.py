"""Working folders for workflow file hand-off, keyed by (run, node, iteration). The engine
gives every sequential node of a run ONE shared folder (node ``"work"``, no iteration) so
their created/edited files accumulate in one place and each stage sees the prior work;
parallel branch forks use per-(branch, iteration) folders so concurrent writers can't
collide before reconciliation. The tree gitignores itself and is pruned to recent runs.

``keyed_work_dir`` is the other way a run gets a working folder: a caller-supplied
``work_key`` names a STABLE folder under ``keys/`` that persists across separate run_ids —
the production entrance for the reuse gate (see ``WorkflowEngine.run``'s ``work_key``
param), since a fresh per-run folder always starts with an empty provenance ledger."""
from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path

from durin.utils.file_lock import cross_process_lock

ARTIFACT_ROOT = ".workflow"

# Subtree of ARTIFACT_ROOT holding keyed (not per-run) working folders. Named once so
# prune_runs can exclude it by identity rather than by re-typing the literal.
KEYS_DIRNAME = "keys"

# How long an idle keyed (work_key) dir survives before prune_runs reaps it.
# A keyed dir has no run_id and so never ages out via `keep` (that retention
# counts RUNS; a keyed dir lives across arbitrarily many runs sharing one
# work_key) — elapsed idle time is the only bound it has at all.
KEYED_WORK_MAX_AGE_DAYS = 30

# Minimum interval between keyed-dir age sweeps. _prune_keyed_dirs's recursive
# mtime walk touches every file under every keys/<workflow>/<key>/ dir, so
# running it on EVERY prune_runs call (i.e. every workflow run start) would
# cost O(total keyed-folder size) per run in a workspace with many keys —
# mirrors channels/email.py's own prune gate, but via an on-disk marker
# (keys/.last-sweep) rather than an in-memory timestamp: prune_runs is called
# fresh per run, sometimes from a different process, so nothing here lives
# long enough to hold one in memory.
KEYED_SWEEP_MIN_INTERVAL_S = 86400   # at most once a day
_SWEEP_MARKER_NAME = ".last-sweep"

_SAFE_KEY_PREFIX_MAX_CHARS = 60
_UNSAFE_KEY_CHARS = re.compile(r"[^a-z0-9._-]")


def safe_key(value: str) -> str:
    """Sanitize an arbitrary caller-supplied string (a workflow name or a work_key)
    into a single filesystem-safe, COLLISION-PROOF path segment:
    ``<sanitized-prefix, capped at 60 chars>-<sha256(the ORIGINAL value)[:8]>``.

    The prefix is lowercased, every character outside ``[a-z0-9._-]`` replaced with
    ``_`` (so ``"Ticket #23124"`` -> ``"ticket__23124"``, one ``_`` per replaced
    character — not collapsed), leading dots stripped (no hidden-file-style names).
    The hash is always appended, over the exact ORIGINAL (case-sensitive, pre-
    sanitize) string — never omitted for an already-safe value — because the prefix
    alone is lossy: two DIFFERENT raw values can sanitize to the identical prefix
    (``"Ticket #1"`` and ``"ticket_#1"`` both become ``"ticket__1"``), and without the
    hash they would silently share one directory — cross-key artifact bleed. The
    8-hex-char suffix (32 bits) makes that collision astronomically unlikely while
    keeping the segment short and mostly human-readable.

    A path separator (``/`` or ``\\``) in the input is rejected outright (``ValueError``)
    rather than merely neutralized: it is the one signal that the caller handed a PATH,
    not an opaque label, and this function's whole job is to guarantee its result is
    exactly one path segment — fail loud instead of silently defusing it into a
    same-level sibling name. Also raised when the sanitized prefix is empty or dots
    only (both are unusable as a directory name) — checked before the hash is ever
    computed, so a bad key never produces a path that merely LOOKS valid."""
    if "/" in value or "\\" in value:
        raise ValueError(f"key {value!r} must not contain a path separator")
    cleaned = _UNSAFE_KEY_CHARS.sub("_", value.lower()).lstrip(".")
    if not cleaned or not cleaned.strip("."):
        raise ValueError(f"key {value!r} sanitizes to an empty or invalid path segment")
    prefix = cleaned[:_SAFE_KEY_PREFIX_MAX_CHARS]
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{suffix}"


def _root(base: str | Path) -> Path:
    root = Path(base) / ARTIFACT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")          # the whole artifact tree ignores itself
    return root


def artifact_dir(base: str | Path, run_id: str, node_id: str, iteration: int | None) -> Path:
    # ``iteration=None`` yields ONE stable folder for the node (a self-looping node
    # accumulates its files there across iterations); an int keeps the per-iteration
    # folders used by linear/fan-out hand-off so re-iterations don't collide.
    d = _root(base) / run_id / node_id
    if iteration is not None:
        d = d / str(iteration)
    d.mkdir(parents=True, exist_ok=True)
    return d


def keyed_work_dir(base: str | Path, workflow_name: str, work_key: str) -> Path:
    """The STABLE working folder for a (workflow, work_key) pair:
    ``.workflow/keys/<safe(workflow_name)>/<safe(work_key)>/work``. Unlike
    ``artifact_dir``'s per-run_id folders, this same path is returned for every run
    sharing this workflow + key — the production entrance for the reuse gate: an
    engine-written ``.provenance.json`` here survives across separate run_ids, so a
    later run's reuse check finds what an earlier one stamped instead of reading an
    always-empty ledger. Raises ``ValueError`` (via ``safe_key``) on an unusable name."""
    d = Path(base) / ARTIFACT_ROOT / KEYS_DIRNAME / safe_key(workflow_name) / safe_key(work_key) / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Lock target name, kept BESIDE the keyed work dir (not inside it) so the ".lock"
# file cross_process_lock derives never shows up as a run artifact — mirrors
# workflow/version_store.py's identical VERSION_LOCK_NAME/version_lock_target pattern.
RUN_LOCK_NAME = ".run"


def keyed_run_lock_target(work_dir: str | Path) -> Path:
    """The cross-process lock target serializing every run sharing *work_dir*'s
    (workflow, work_key) folder — one shared folder, one writer at a time. Two
    runs racing on the same key would otherwise both read "no provenance yet",
    both dispatch, and both write ``output_file``, the second silently clobbering
    the first's (a lost update) — reachable via, e.g., a loop with
    ``concurrency: "parallel"`` double-firing on the same correlate key."""
    return Path(work_dir).parent / RUN_LOCK_NAME


def _newest_mtime(path: Path) -> float:
    """The most recent mtime among every file under *path* (recursively), or
    *path*'s own mtime if it holds no files. A directory's own mtime only
    bumps when its DIRECT children change, so a write two levels down
    (``keys/<wf>/<key>/work/report.md``) never bubbles up to
    ``keys/<wf>/<key>/``'s own mtime — walking the whole subtree is the only
    reliable "how recently was anything in here touched" signal."""
    newest = path.stat().st_mtime
    for p in path.rglob("*"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


def _prune_keyed_dirs(keys_root: Path, max_age_days: float,
                       protect: set[tuple[str, str]] | None = None) -> None:
    """Age-based retention for ``keys/<workflow>/<key>/`` dirs: prune_runs's
    run-count retention (``keep``) does not apply here — a keyed dir isn't a
    run and has no run_id — so elapsed idle time is the only bound.

    Debounced via an on-disk marker (``KEYS_DIRNAME/_SWEEP_MARKER_NAME``):
    the recursive mtime walk below touches every file under every key dir, so
    it runs at most once per ``KEYED_SWEEP_MIN_INTERVAL_S`` regardless of how
    often the caller (every workflow run) invokes it.

    ``protect`` names raw ``(workflow_name, work_key)`` pairs — pre-safe_key,
    e.g. what ``run_log.live_work_keys`` returns — that survive unconditionally,
    regardless of age OR lock state. This is NOT redundant with the per-dir
    lock check below: a PARKED (``needs_input``) run releases its keyed dir's
    cross-process lock the instant it parks (``WorkflowEngine.run``'s lock is
    scoped to one call, not to the run's whole paused lifetime), so a dir can
    look both "idle past the age bound" and "not currently locked" while still
    belonging to a resumable run nobody has answered in 30+ days — exactly the
    same "live" definition ``run_log.live_run_ids`` uses to protect per-run
    folders, applied here to keyed ones. A dir NOT named in ``protect`` still
    gets the per-dir lock check: a live run in progress (not parked) holds its
    lock the whole time, so it must never lose its working folder mid-flight
    either, however old its last write looked. One bad key dir (a permissions
    problem, a race) is logged nowhere and simply left for next time — it must
    never abort the sweep for every other key.
    """
    if not keys_root.is_dir():
        return
    marker = keys_root / _SWEEP_MARKER_NAME
    try:
        last_swept = marker.stat().st_mtime
    except OSError:
        last_swept = 0.0   # no marker yet — the first sweep is always due
    if time.time() - last_swept < KEYED_SWEEP_MIN_INTERVAL_S:
        return
    protected_dirs: set[tuple[str, str]] = set()
    for wf, wk in (protect or set()):
        try:
            protected_dirs.add((safe_key(wf), safe_key(wk)))
        except ValueError:
            continue   # an unsanitizable protect entry can't name a real dir anyway
    cutoff = time.time() - max_age_days * 86400
    for workflow_dir in keys_root.iterdir():
        if not workflow_dir.is_dir():
            continue
        for key_dir in workflow_dir.iterdir():
            if not key_dir.is_dir():
                continue
            if (workflow_dir.name, key_dir.name) in protected_dirs:
                continue
            try:
                if _newest_mtime(key_dir) >= cutoff:
                    continue
                try:
                    with cross_process_lock(keyed_run_lock_target(key_dir / "work"), timeout=0):
                        pass
                except TimeoutError:
                    continue   # a run is currently holding this key — never remove it
                shutil.rmtree(key_dir, ignore_errors=True)
            except OSError:
                continue
    try:
        marker.touch()   # stamped AFTER a completed sweep, not before
    except OSError:
        pass


def prune_runs(base: str | Path, keep: int = 20, protect: set[str] | None = None,
               protect_keyed: set[tuple[str, str]] | None = None) -> None:
    """Best-effort: keep the `keep` most-recent run subtrees, remove older ones.

    ``protect`` names run ids that are never deleted and never counted toward
    ``keep`` — the caller passes the runs still executing or paused awaiting
    resume. Age alone cannot protect a live run: a long node freezes its
    folder's mtime, so enough newer runs starting during it would push the
    live run out of the retained window and delete its files mid-run.

    ``KEYS_DIRNAME`` (``keys/``) is never a candidate for THIS run-count
    retention: it holds keyed working folders, not per-run ones, and has no
    run_id to count against `keep`. It still ages out on its own clock —
    see ``_prune_keyed_dirs``/``KEYED_WORK_MAX_AGE_DAYS`` — an idle keyed dir
    past that age is removed here too, lock files included, unless its run
    lock is currently held or it is named in ``protect_keyed`` (raw
    ``(workflow_name, work_key)`` pairs — see ``_prune_keyed_dirs`` for why
    the lock check alone is not enough for a parked run).
    """
    try:
        root = Path(base) / ARTIFACT_ROOT
        if not root.is_dir():
            return
        protected = protect or set()
        runs = sorted((p for p in root.iterdir()
                      if p.is_dir() and p.name != KEYS_DIRNAME and p.name not in protected),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in runs[keep:]:
            shutil.rmtree(old, ignore_errors=True)
        _prune_keyed_dirs(root / KEYS_DIRNAME, KEYED_WORK_MAX_AGE_DAYS, protect_keyed)
    except OSError:
        pass
