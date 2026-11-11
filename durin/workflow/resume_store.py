"""What a run's next step receives, kept whole, so a run whose process died can be
entered again with exactly that input.

The run's record names the step to enter next (its ``position``: a step id and a
sequence number that grows every time the position changes) but keeps text only
in capped copies, which are for its readers. The text itself lives here, in files
that belong to the run: ``<workspace>/.workflow/<run_id>/resume``. It is one of
the run's folders under ``.workflow/``, so the run-folder pruning (``keep_runs``)
removes it and spares it while the run is live. For a run with a per-run working
folder it sits beside that folder and goes with it. A ``work_key`` run's working
folder lives under ``.workflow/keys/`` and ages out on its own clock instead, so
the two can be pruned at different times.

- ``input-<seq>.json``: the step a position names and the edge text it receives.
  The walk writes it BEFORE the record that points at it, so a record never names
  an input that was not written. The input of the position before stays too: if
  the process dies between the two writes, the record still names that one.
- ``source-<hash>.json``: the last output of each step another step reads by name
  (``inputs_from``, ``list_from``, ``branches_from``). Named by a hash of the step
  id, because a step id is free text and not always a usable file name; the id
  itself is kept inside the file.

Each file is written to a temporary file beside it and renamed over it, so a
reader sees the previous file or the new one, never a torn one. One walk writes
these files at a time (the run's own), so no lock is needed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text
from durin.workflow.artifacts import ARTIFACT_ROOT

_INPUT_PREFIX = "input-"
_SOURCE_PREFIX = "source-"


def resume_dir(workspace: str | Path, run_id: str) -> Path:
    """The run's resume folder. Not created here: only a write creates it."""
    return Path(workspace) / ARTIFACT_ROOT / run_id / "resume"


def _input_path(workspace: str | Path, run_id: str, seq: int) -> Path:
    return resume_dir(workspace, run_id) / f"{_INPUT_PREFIX}{int(seq)}.json"


def _source_path(workspace: str | Path, run_id: str, node_id: str) -> Path:
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:32]
    return resume_dir(workspace, run_id) / f"{_SOURCE_PREFIX}{digest}.json"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_input(workspace: str | Path, run_id: str, seq: int, node_id: str,
               upstream: str | None) -> None:
    """Write what the step at position ``seq`` receives, then drop every input
    older than the one before it: only the position the record names and the
    one it named before can still be read through a record."""
    atomic_write_text(_input_path(workspace, run_id, seq),
                      json.dumps({"node": node_id, "upstream": upstream}))
    for path in resume_dir(workspace, run_id).glob(f"{_INPUT_PREFIX}*.json"):
        try:
            k = int(path.stem[len(_INPUT_PREFIX):])
        except ValueError:
            continue
        if k < seq - 1:
            path.unlink(missing_ok=True)


def load_input(workspace: str | Path, run_id: str, seq: int) -> tuple[str, str | None] | None:
    """``(node_id, upstream)`` stored for position ``seq``, or None when that file
    does not exist or cannot be read."""
    data = _read_json(_input_path(workspace, run_id, seq))
    if data is None or not isinstance(data.get("node"), str):
        return None
    return data["node"], data.get("upstream")


def save_source(workspace: str | Path, run_id: str, node_id: str, output: str) -> None:
    """Keep the whole last output of a step another step reads by name; a later
    output of the same step replaces it."""
    atomic_write_text(_source_path(workspace, run_id, node_id),
                      json.dumps({"node": node_id, "output": output}))


def load_sources(workspace: str | Path, run_id: str) -> dict[str, str]:
    """``{step id: whole last output}`` of every stored source; ``{}`` when
    nothing was stored."""
    folder = resume_dir(workspace, run_id)
    if not folder.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in folder.glob(f"{_SOURCE_PREFIX}*.json"):
        data = _read_json(path)
        if data and isinstance(data.get("node"), str) and isinstance(data.get("output"), str):
            out[data["node"]] = data["output"]
    return out
