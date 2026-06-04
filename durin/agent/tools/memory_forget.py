"""memory_forget tool — archive a memory entry + drop its index rows.

The agent's in-band, index-safe deletion path. Without it the only way to
remove an entry was a raw shell `rm`, which leaves the FTS + vector
indices pointing at a missing file (orphan rows the auto-repair can't
reconstruct). This tool routes through the shared `forget_entry` helper so
the markdown move and the index cleanup stay consistent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.memory.forget import ForgetError, forget_entry, parse_memory_uri

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    uri=StringSchema(
        "Entry URI in 'memory/<class>/<id>' form — exactly the `uri` field "
        "memory_search returns (e.g. 'memory/stable/0922d2931b46')."
    ),
    reason=StringSchema(
        "Optional short reason recorded in the archive frontmatter "
        "(e.g. 'duplicate', 'outdated'). Defaults to 'agent_forget'."
    ),
    required=["uri"],
    description=(
        "Remove a memory entry you no longer want surfaced. Archives it to "
        "memory/archive/<class>/<id>.md (reversible) and removes its search "
        "index rows so it stops appearing in memory_search.\n\n"
        "This is the ONLY correct way to delete a memory entry — never rm or "
        "move files under memory/ via shell, which leaves the search indices "
        "pointing at a missing file.\n\n"
        "Pass `uri` exactly as memory_search returned it. Refuses entity "
        "pages (memory/entities/...): those have their own absorb/revert "
        "lifecycle."
    ),
)


@tool_parameters(_PARAMETERS)
class MemoryForgetTool(Tool):
    """Archive a memory entry and drop its FTS + vector index rows."""

    config_key = "memory"

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "memory_forget"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        uri = (kwargs.get("uri") or "").strip()
        reason = (kwargs.get("reason") or "agent_forget").strip() or "agent_forget"
        if not uri:
            return {"error": "uri is required (memory/<class>/<id>)"}
        try:
            dest = forget_entry(self._workspace, uri, reason=reason)
        except ForgetError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_forget failed for %s: %s", uri, exc)
            return {"error": f"forget failed: {exc}"}
        try:
            rel = str(dest.relative_to(self._workspace))
        except ValueError:
            rel = str(dest)
        class_name, _ = parse_memory_uri(uri)
        emit_tool_event(
            "memory.forget",
            {"uri": uri, "class_name": class_name, "reason": reason},
        )
        return {"uri": uri, "archived_to": rel, "status": "forgotten"}
