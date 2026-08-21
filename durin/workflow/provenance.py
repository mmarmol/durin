"""Artifact provenance for workflow work folders.

`.provenance.json` maps each engine-written output file to WHO produced it AND
UNDER WHAT CONDITIONS — node definition hash, resolved model/provider,
generation params hash, a hash of the composed input the node was dispatched
with, and a hash of the artifact's own content — so a later run can decide
"identical producer, identical input, unchanged content → reusable" and
anything else re-runs. Written only by the engine; a missing or corrupt file
means "unknown", which must always be treated as not-reusable."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from durin.utils.file_lock import cross_process_lock

FILENAME = ".provenance.json"

# BELT: record()/drop() are the only writers of FILENAME, but each does its own
# unlocked load-mutate-write — two co-writers (any future caller alongside the
# engine, or record() racing its own drop() cleanup) could still lose an update.
# Kept BESIDE the work dir (not inside it) so the ".lock" file cross_process_lock
# derives never shows up as a run artifact — same reasoning as artifacts.py's
# RUN_LOCK_NAME. Short timeout: this guards a quick in-memory-then-disk write,
# never a slow operation — a long hold here means something is genuinely stuck.
_LOCK_NAME = ".provenance-write"
_LOCK_TIMEOUT_S = 10.0


def _lock_target(work_dir: Path) -> Path:
    return Path(work_dir).parent / _LOCK_NAME


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def node_hash(node_spec: dict) -> str:
    return hashlib.sha256(_canonical(node_spec).encode("utf-8")).hexdigest()


# Node-definition keys that do NOT determine an artifact's content — pure graph
# wiring or display metadata. A DENYLIST, not an allowlist: reuse_hash() hashes
# everything else, so a field this set doesn't name (including one added to
# WorkNode after this was written) still participates in the hash — conservative
# by default, since under-including a content-affecting field risks a stale
# reuse, while over-including a wiring field only costs an occasional needless
# re-run.
REUSE_IGNORED_KEYS = frozenset({
    "id",          # the node's own graph identity, not part of what it produces
    "title",       # human display label only
    "next",        # linear routing edge — wiring, not content
    "on_pass",     # binary routing edge — wiring, not content
    "on_fail",     # binary routing edge — wiring, not content
    "cases",       # multi-way routing edges — wiring, not content
    "detached",    # launch-and-continue scheduling — doesn't change what runs
    "max_visits",  # loop-cap wiring, not content
    "reuse",       # the reuse opt-in flag itself — toggling it must not
                   # invalidate the very artifact it is about to be compared against
})


def reuse_hash(raw_node_spec: dict) -> str:
    """Hash of the node-definition fields that determine an artifact's content:
    everything in the raw spec EXCEPT the pure-wiring/display keys in
    REUSE_IGNORED_KEYS. Notably this now includes inputs_from (composes the
    node's entire input via the engine), mcps, context, session, max_reentries,
    and reentry_prompt — all content-determining, previously missed by an
    allowlist that named only a few fields explicitly."""
    return node_hash({k: v for k, v in raw_node_spec.items() if k not in REUSE_IGNORED_KEYS})


def node_identity(node) -> dict:
    """A JSON-serializable identity for one workflow-graph node, for spec_hash
    (the whole-graph signal, unlike reuse_hash's narrower projection — spec_hash
    is deliberately sensitive to routing/wiring too, since it answers "did the
    graph change at all", not "is a specific artifact still reusable").

    A WorkNode's ``raw`` spec dict IS its identity — the authored source. Script,
    subworkflow, and parallel nodes carry no ``raw`` field, so their parsed
    dataclass fields stand in instead: still an exact, order-independent identity,
    since the on-disk definition determines them uniquely. Falls back to just
    id+kind only if even that fails (a non-dataclass double in a test)."""
    raw = getattr(node, "raw", None)
    if raw:
        return raw
    try:
        return dataclasses.asdict(node)
    except (TypeError, ValueError):
        return {"id": getattr(node, "id", None), "kind": type(node).__name__}


def params_hash(generation) -> str:
    keys = ("max_tokens", "temperature", "reasoning_effort", "top_p", "top_k")
    payload = {k: getattr(generation, k, None) for k in keys}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def input_hash(task: str | None, node_input: str | None) -> str:
    """Hash of the exact (task, composed-upstream-input) pair a node was
    dispatched with — the same two values NodeRunRequest carries as ``task``/
    ``upstream_output``. The reuse gate compares this against a stored entry so
    an identical producer fed DIFFERENT input (a changed task, new upstream
    text, or loop-back feedback) is never mistaken for the same call."""
    payload = {"task": task, "upstream_output": node_input}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def content_sha256(text: str) -> str:
    """Hash of an artifact's exact text content, as written to disk. The reuse
    gate compares this against a stored entry so content edited, restored, or
    otherwise drifted after being stamped is never reused just because its
    producer's identity still matches."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def durin_version() -> str | None:
    """The installed durin-agent distribution version, or None when it cannot be
    resolved (e.g. running from source with no installed distribution). Best-effort
    identification stamped into provenance entries and run manifests — never raises."""
    try:
        import importlib.metadata
        return importlib.metadata.version("durin-agent")
    except Exception:  # noqa: BLE001 - version stamping is a nicety, never fatal
        return None


def load(work_dir: Path) -> dict[str, dict]:
    try:
        raw = json.loads((Path(work_dir) / FILENAME).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def record(work_dir: Path, filename: str, entry: dict) -> None:
    with cross_process_lock(_lock_target(work_dir), timeout=_LOCK_TIMEOUT_S):
        data = load(work_dir)
        data[filename] = entry
        (Path(work_dir) / FILENAME).write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def drop(work_dir: Path, filename: str) -> None:
    """Best-effort removal of *filename*'s entry. Used when a fresh artifact
    write's ``record()`` call itself fails: a stale (or now-inaccurate) entry
    for the same filename must never be left standing over content that just
    changed, or a later run could treat old metadata as still describing what
    is on disk now. Never raises — a cleanup failure here must not compound the
    record() failure it exists to contain. Shares record()'s lock target: a
    drop() racing a record() for the SAME work_dir (record() calls drop() on its
    own failure path) must serialize too."""
    try:
        with cross_process_lock(_lock_target(work_dir), timeout=_LOCK_TIMEOUT_S):
            data = load(work_dir)
            if filename in data:
                del data[filename]
                (Path(work_dir) / FILENAME).write_text(
                    json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001 - cleanup is best-effort, never fatal
        pass
