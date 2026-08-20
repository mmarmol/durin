"""Artifact provenance for workflow work folders.

`.provenance.json` maps each engine-written output file to WHO produced it —
node definition hash, resolved model/provider, generation params hash — so a
later run can decide "identical producer → reusable" and anything else re-runs.
Written only by the engine; a missing or corrupt file means "unknown", which
must always be treated as not-reusable."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

FILENAME = ".provenance.json"


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def node_hash(node_spec: dict) -> str:
    return hashlib.sha256(_canonical(node_spec).encode("utf-8")).hexdigest()


# The node-definition fields that determine what an artifact's content WOULD be —
# deliberately excludes routing/wiring fields (next, on_pass, on_fail, cases,
# detached, inputs_from, id, title, ...) so editing a node's place in the graph
# never invalidates a reusable artifact.
REUSE_RELEVANT_KEYS = ("prompt", "output_schema", "skills", "mode", "tools",
                       "model", "persona", "max_turns", "kind")


def reuse_hash(raw_node_spec: dict) -> str:
    """Hash of the node-definition fields that determine an artifact's content.
    Routing-only edits (next / on_fail / detached wiring) deliberately do not
    change it, so they never invalidate reuse."""
    return node_hash({k: raw_node_spec.get(k) for k in REUSE_RELEVANT_KEYS})


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
    data = load(work_dir)
    data[filename] = entry
    (Path(work_dir) / FILENAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
