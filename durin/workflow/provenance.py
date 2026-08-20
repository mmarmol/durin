"""Artifact provenance for workflow work folders.

`.provenance.json` maps each engine-written output file to WHO produced it —
node definition hash, resolved model/provider, generation params hash — so a
later run can decide "identical producer → reusable" and anything else re-runs.
Written only by the engine; a missing or corrupt file means "unknown", which
must always be treated as not-reusable."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FILENAME = ".provenance.json"


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def node_hash(node_spec: dict) -> str:
    return hashlib.sha256(_canonical(node_spec).encode("utf-8")).hexdigest()


def params_hash(generation) -> str:
    keys = ("max_tokens", "temperature", "reasoning_effort", "top_p", "top_k")
    payload = {k: getattr(generation, k, None) for k in keys}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


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
