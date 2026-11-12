"""What a run's next step receives, kept whole, so a run whose process died can be
entered again with exactly that input.

The run's record names the step to enter next (its ``position``: a step id and a
sequence number that grows every time the position changes) but keeps text only
in capped copies, which are for its readers. The text itself lives here, in a
folder of files that belongs to the run and sits beside its working folder, so it
is pruned with that folder and spared with it while the run is live:

- a run without a work key: ``<workspace>/.workflow/<run_id>/resume``, beside the
  run's own ``work`` folder;
- a run with a work key: ``<keyed folder>/resume/<run_id>``, where the keyed folder
  is the parent of the shared ``work`` folder (``.workflow/keys/<wf>/<key>``). One
  folder per run, since many runs share a key; the keyed sweep removes it with the
  key's folder, and the run takes no slot of the per-run retention.

``store_dir`` gives the folder to a writer that knows how the run was started;
``store_dir_of`` gives the same folder to a reader holding only the run's record.
A run that ends in a state that cannot be resumed (completed, exhausted,
cancelled) has its folder removed (``discard``).

- ``input-<seq>.json``: the step a position names and the edge text it receives.
  The walk writes it BEFORE the record that points at it, so a record never names
  an input that was not written. The input of the position before stays too: if
  the process dies between the two writes, the record still names that one.
- ``source-<hash>.json``: the last output of each step another step reads by name
  (``inputs_from``, ``list_from``, ``branches_from``), an empty one included.
  Named by a hash of the step id, because a step id is free text and not always a
  usable file name; the id itself is kept inside the file.

Each file is written to a temporary file beside it and renamed over it, so a
reader sees the previous file or the new one, never a torn one. One walk writes
these files at a time (the run's own), so no lock is needed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text
from durin.workflow.artifacts import ARTIFACT_ROOT

_INPUT_PREFIX = "input-"
_SOURCE_PREFIX = "source-"
_DIRNAME = "resume"


def store_dir(workspace: str | Path, run_id: str, *, work_dir: str | None = None,
              keyed: bool = False) -> Path:
    """The folder holding run ``run_id``'s resume files. ``keyed`` is True for a
    run whose working folder ``work_dir`` is a work key's shared folder: the files
    then sit beside that folder, one folder per run. Otherwise they sit in the
    run's own ``.workflow/<run_id>/``. Not created here: only a write creates it."""
    if keyed and work_dir:
        return Path(work_dir).parent / _DIRNAME / run_id
    return Path(workspace) / ARTIFACT_ROOT / run_id / _DIRNAME


def store_dir_of(workspace: str | Path, record: dict) -> Path:
    """The same folder as ``store_dir``, from a run's record: the record's
    ``work_key`` says whether the run was keyed and its ``work_dir`` is then the
    key's shared working folder; ``run_id`` names the run."""
    work_key = record.get("work_key")
    work_dir = record.get("work_dir")
    return store_dir(workspace, record["run_id"], work_dir=work_dir,
                     keyed=bool(work_key and work_dir))


def _input_path(folder: Path, seq: int) -> Path:
    return folder / f"{_INPUT_PREFIX}{int(seq)}.json"


def _source_path(folder: Path, node_id: str) -> Path:
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:32]
    return folder / f"{_SOURCE_PREFIX}{digest}.json"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_input(store_dir: Path, seq: int, node_id: str, upstream: str | None) -> None:
    """Write what the step at position ``seq`` receives, then drop every input
    older than the one before it: only the position the record names and the
    one it named before can still be read through a record."""
    folder = Path(store_dir)
    atomic_write_text(_input_path(folder, seq),
                      json.dumps({"node": node_id, "upstream": upstream}))
    for path in folder.glob(f"{_INPUT_PREFIX}*.json"):
        try:
            k = int(path.stem[len(_INPUT_PREFIX):])
        except ValueError:
            continue
        if k < seq - 1:
            path.unlink(missing_ok=True)


def load_input(store_dir: Path, seq: int) -> tuple[str, str | None] | None:
    """``(node_id, upstream)`` stored for position ``seq``, or None when that file
    does not exist or cannot be read."""
    data = _read_json(_input_path(Path(store_dir), seq))
    if data is None or not isinstance(data.get("node"), str):
        return None
    return data["node"], data.get("upstream")


def save_source(store_dir: Path, node_id: str, output: str) -> None:
    """Keep the whole last output of a step another step reads by name; a later
    output of the same step replaces it, an empty one included."""
    atomic_write_text(_source_path(Path(store_dir), node_id),
                      json.dumps({"node": node_id, "output": output}))


def load_sources(store_dir: Path) -> dict[str, str]:
    """``{step id: whole last output}`` of every stored source; ``{}`` when
    nothing was stored."""
    folder = Path(store_dir)
    if not folder.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in folder.glob(f"{_SOURCE_PREFIX}*.json"):
        data = _read_json(path)
        if data and isinstance(data.get("node"), str) and isinstance(data.get("output"), str):
            out[data["node"]] = data["output"]
    return out


def discard(store_dir: Path) -> None:
    """Remove a run's resume folder, once the run ended in a state that cannot be
    resumed. Best-effort: a folder that is gone or cannot be removed is left."""
    shutil.rmtree(Path(store_dir), ignore_errors=True)
