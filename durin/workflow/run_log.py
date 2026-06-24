"""Per-run records for workflow self-improvement.

``WorkflowResult`` is discarded after a run, but its per-node trace (iteration counts,
decision pass/fail, final status) is exactly the diagnostic signal dream needs. So each
run writes a small record. Records live BESIDE the workflow definitions
(``<workspace>/workflows-runs/<name>/<run_id>.json``), never inside ``workflows/`` —
the version store snapshots that directory wholesale, and run records are not versioned
definition state.

Each record file is unique (``<run_id>.json``), so concurrent runs append without an
RMW race. A per-workflow cursor marks how far the dream pass has consumed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def runs_root(workspace: str | Path) -> Path:
    return Path(workspace) / "workflows-runs"


def _wf_dir(workspace: str | Path, name: str) -> Path:
    return runs_root(workspace) / name


def write_run(workspace: str | Path, name: str, result, *, ts: float | None = None) -> Path:
    """Persist a workflow run's diagnostic trace. Returns the record path."""
    d = _wf_dir(workspace, name)
    d.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": result.run_id,
        "workflow": name,
        "status": result.status,
        "ts": ts if ts is not None else time.time(),
        "runs": [
            {"node_id": r.node_id, "iteration": r.iteration, "passed": r.passed}
            for r in result.runs
        ],
    }
    path = d / f"{result.run_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def read_runs_since(workspace: str | Path, name: str, cursor_ts: float = 0.0) -> list[dict]:
    """All run records for *name* newer than *cursor_ts*, oldest-first."""
    d = _wf_dir(workspace, name)
    if not d.is_dir():
        return []
    out: list[dict] = []
    for f in d.glob("*.json"):
        if f.name == ".cursor.json":
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("ts", 0.0) > cursor_ts:
            out.append(rec)
    out.sort(key=lambda r: r.get("ts", 0.0))
    return out


def read_cursor(workspace: str | Path, name: str) -> float:
    f = _wf_dir(workspace, name) / ".cursor.json"
    try:
        return float(json.loads(f.read_text(encoding="utf-8")).get("ts", 0.0))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0.0


def advance_cursor(workspace: str | Path, name: str, ts: float) -> None:
    d = _wf_dir(workspace, name)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".cursor.json").write_text(json.dumps({"ts": ts}), encoding="utf-8")


def workflow_names_with_runs(workspace: str | Path) -> list[str]:
    """Names of workflows that have at least one run record."""
    root = runs_root(workspace)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())
