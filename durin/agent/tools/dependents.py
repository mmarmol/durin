"""dependents tool — what references a skill, a workflow script, or a workflow.

The definition graph was only ever readable forwards: a workflow says which
skills and scripts it uses, but nothing could answer the reverse. Without it an
autonomous pass has to discover a dependency by being refused; with it, it can
check first and route the change to the user instead of attempting it.

Read-only. The refusal itself lives in the store (see `_dependency_refusal`), so
skipping this tool cannot get an unsafe mutation through.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    kind=StringSchema(
        "What the name refers to.", enum=["skill", "script", "workflow"],
    ),
    name=StringSchema(
        "The artifact's name: a skill name, a script file name as a script node "
        "spells it (e.g. 'resolve-org.py'), or a workflow name."
    ),
    required=["kind", "name"],
    description=(
        "List what depends on a skill, a workflow script, or a workflow — the "
        "workflow nodes that name it and the loops that run it. Check before "
        "rewriting, merging or retiring something: a workflow references a skill "
        "by name, so changing one changes what that workflow does, and removing "
        "one leaves the reference dangling."
    ),
)


@tool_parameters(_PARAMETERS)
class DependentsTool(Tool):
    """dependents tool — the reverse edges of the definition graph."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "dependents"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "DependentsTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> str:
        from durin.registry_graph import dependents_of

        kind = str(kwargs.get("kind", ""))
        name = str(kwargs.get("name", ""))
        if kind not in ("skill", "script", "workflow"):
            return json.dumps({"error": "kind must be skill, script or workflow"})
        if not name:
            return json.dumps({"error": "name is required"})

        deps = dependents_of(self._workspace, **{kind: name})
        return json.dumps({
            "kind": kind,
            "name": name,
            "dependents": [
                {"kind": d.kind, "name": d.name, "via": d.via, "where": d.where}
                for d in deps
            ],
            "note": (
                "nothing depends on this — safe to change on its own merits"
                if not deps else
                "changing this changes what these do; removing it breaks them"
            ),
        })
