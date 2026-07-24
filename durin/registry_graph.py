"""What references a registry artifact — the reverse of the definition graph.

Workflows and loops name the things they depend on: a work node names `skills`,
a script node names a file under `workflows/scripts/`, a sub-flow node names
another workflow, and a loop names the workflow it runs. Those edges were only
ever read forwards (to run something); nothing could answer the reverse question
— *who depends on this?* — so a mutation could remove or rewrite an artifact out
from under a workflow that references it by name.

Reads raw JSON rather than the parsed models on purpose: this answers questions
about definitions that may be mid-edit or invalid, and a barrier that raises on a
malformed sibling would fail open exactly when the tree is messiest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Dependent:
    kind: str    # "workflow" | "loop" — what does the referencing
    name: str    # its name
    via: str     # the edge: "skills" | "script" | "subworkflow" | "workflow"
    where: str   # node id inside a workflow; "" when the whole artifact refers


def _definitions(directory: Path) -> list[tuple[str, dict]]:
    """(name, raw definition) for every readable JSON file. Unreadable or
    malformed files are skipped — never fatal for a dependency question."""
    out: list[tuple[str, dict]] = []
    if not directory.is_dir():
        return out
    for p in sorted(directory.glob("*.json")):
        if p.name.startswith("."):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append((p.stem, data))
    return out


def dependents_of(
    workspace: str | Path,
    *,
    skill: str | None = None,
    script: str | None = None,
    workflow: str | None = None,
) -> list[Dependent]:
    """Every workflow node and loop that names the given artifact.

    Exactly one of ``skill`` / ``script`` / ``workflow`` identifies the target.
    Returns an empty list when nothing references it — the caller decides
    whether that permits a mutation.
    """
    ws = Path(workspace)
    out: list[Dependent] = []

    for name, definition in _definitions(ws / "workflows"):
        nodes = definition.get("nodes")
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id", ""))
            if skill is not None:
                named = node.get("skills")
                if isinstance(named, list) and skill in named:
                    out.append(Dependent("workflow", name, "skills", node_id))
            if script is not None and node.get("script") == script:
                out.append(Dependent("workflow", name, "script", node_id))
            if (workflow is not None and node.get("kind") == "subworkflow"
                    and node.get("workflow") == workflow):
                out.append(Dependent("workflow", name, "subworkflow", node_id))

    if workflow is not None:
        for name, definition in _definitions(ws / "loops"):
            if definition.get("workflow") == workflow:
                out.append(Dependent("loop", name, "workflow", ""))

    return out


def describe(dependents: list[Dependent]) -> str:
    """One-line, human-readable summary for an error or a suggestion card."""
    return ", ".join(
        f"{d.kind} {d.name}" + (f" (node {d.where})" if d.where else "")
        for d in dependents
    )
