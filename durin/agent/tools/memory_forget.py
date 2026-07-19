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
from durin.memory.forget import (
    ForgetError,
    forget_entry,
    forget_reference,
    parse_memory_uri,
)

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    uri=StringSchema(
        "What to forget, exactly as memory_search / the Library return it: an "
        "entry URI in 'memory/<class>/<id>' form (e.g. "
        "'memory/stable/0922d2931b46'), or a 'reference:<slug>' to forget a "
        "whole ingested document from the Library."
    ),
    reason=StringSchema(
        "Optional short reason recorded in the archive frontmatter "
        "(e.g. 'duplicate', 'outdated'). Defaults to 'agent_forget'."
    ),
    required=["uri"],
    description=(
        "Remove something you no longer want surfaced — a memory entry OR an "
        "ingested Library document. Archives it (reversible) and removes its "
        "search index rows so it stops appearing in memory_search.\n\n"
        "This is the ONLY correct way to delete memory — never rm or move "
        "files under memory/ via shell, which leaves the search indices "
        "pointing at a missing file (orphan rows).\n\n"
        "Pass `uri` exactly as it was returned: a 'memory/<class>/<id>' entry, "
        "or a 'reference:<slug>' to forget an ingested document (the whole "
        "doc: its chunks and index rows go too). Refuses entity pages "
        "(memory/entities/...): those have their own absorb/revert lifecycle."
    ),
)


@tool_parameters(_PARAMETERS)
class MemoryForgetTool(Tool):
    """Archive a memory entry and drop its FTS + vector index rows."""

    # Core-only: destructive archival stays a primary-agent decision.
    _scopes = {"core"}

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
            return {"error": "uri is required (memory/<class>/<id> or reference:<slug>)"}

        # Ingested Library documents are `reference:<slug>` — a different
        # archive + index-cleanup path than the memory/<class>/<id> entries.
        if uri.startswith("reference:"):
            return self._forget_reference(uri, reason)

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

    def _forget_reference(self, uri: str, reason: str) -> dict[str, Any]:
        """Forget an ingested Library document (``reference:<slug>``)."""
        try:
            dest = forget_reference(self._workspace, uri, reason=reason)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_forget failed for %s: %s", uri, exc)
            return {"error": f"forget failed: {exc}"}
        if dest is None:
            return {"error": f"document not found: {uri}"}
        try:
            rel = str(dest.relative_to(self._workspace))
        except ValueError:
            rel = str(dest)
        emit_tool_event(
            "memory.forget",
            {"uri": uri, "class_name": "reference", "reason": reason},
        )
        return {"uri": uri, "archived_to": rel, "status": "forgotten"}
