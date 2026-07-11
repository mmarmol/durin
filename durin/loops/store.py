"""Loop definition persistence: one JSON file per loop under <workspace>/loops/.

Same model as workflow definitions: files are the truth, atomic full-file
rewrite; a per-loop cross-process lock guards save/delete because the webui,
the agent tool, and the CLI may write concurrently — writers touching
different loops never block each other, only same-name writers serialize.
"""

from __future__ import annotations

import json
from pathlib import Path

from durin.loops.spec import LoopNotFound, LoopSpec, loop_to_dict, parse_loop
from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock


def loops_dir(workspace: str | Path) -> Path:
    return Path(workspace) / "loops"


def _path(workspace: str | Path, name: str) -> Path:
    return loops_dir(workspace) / f"{name}.json"


def load_loop(workspace: str | Path, name: str) -> LoopSpec:
    p = _path(workspace, name)
    if not p.exists():
        raise LoopNotFound(f"loop '{name}' not found")
    return parse_loop(json.loads(p.read_text(encoding="utf-8")))


def list_loops(workspace: str | Path) -> list[LoopSpec]:
    d = loops_dir(workspace)
    if not d.is_dir():
        return []
    out: list[LoopSpec] = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(parse_loop(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue  # malformed files are skipped, never fatal for listing
    return out


def save_loop(workspace: str | Path, spec: LoopSpec) -> None:
    d = loops_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    with cross_process_lock(loops_dir(workspace) / spec.name):
        atomic_write_text(_path(workspace, spec.name), json.dumps(loop_to_dict(spec), indent=2))


def delete_loop(workspace: str | Path, name: str) -> None:
    p = _path(workspace, name)
    with cross_process_lock(loops_dir(workspace) / name):
        if not p.exists():
            raise LoopNotFound(f"loop '{name}' not found")
        p.unlink()
