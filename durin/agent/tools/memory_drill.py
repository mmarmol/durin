"""memory_drill tool — resolve memory URIs to their full content.

Accepts either a single ``uri`` (legacy shape) or a list of ``uris``
in one call. The list form (audit H9 consolidation, 2026-05-29)
replaces the standalone ``memory_drill_batch`` tool: same payload,
single round-trip, fewer top-level tools surfaced to the LLM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import (
    ArraySchema,
    StringSchema,
    tool_parameters_schema,
)
from durin.memory.drill import DrillError, drill

# Cap on the number of URIs per call. 10 chosen because
# ``memory_search.limit`` defaults to 10 — drilling more than the
# default search returns is almost always a sign the agent should
# narrow the query instead of dumping bodies into context.
MAX_BATCH_URIS: int = 10

_URI_DESCRIPTION = (
    "Markdown URI such as 'sessions/<key>.md#turn-42', "
    "'ingested/<id>/source.md#section-3', 'memory/<class>/<id>', or "
    "'skills/<slug>/SKILL.md'."
)

_PARAMETERS = tool_parameters_schema(
    uri=StringSchema(
        _URI_DESCRIPTION + " Use this OR ``uris`` (not both).",
    ),
    uris=ArraySchema(
        items=StringSchema(_URI_DESCRIPTION),
        description=(
            f"List of URIs to drill in a single call. "
            f"Maximum {MAX_BATCH_URIS} per call. Results come back in "
            "the same order."
        ),
    ),
    description=(
        # Canonical text per `docs/architecture/memory/06_prompts_and_instructions.md` §3.4.
        "Read the full content of one or more memory items by URI.\n\n"
        "Pass either ``uri`` (single string) for one item, or ``uris`` "
        f"(array, up to {MAX_BATCH_URIS}) for multiple items in one "
        "round-trip. With ``uris`` the response carries one "
        "``{uri, content}`` record per request in the same order, "
        "plus an ``error`` field on entries that failed — individual "
        "failures don't abort the batch.\n\n"
        "Use this ONLY when the corresponding memory_search result "
        "block is marked ``preview N/M`` in its section header — N "
        "chars were shown, M chars exist — i.e. more body is "
        "available beyond what you already have. Drill in that case "
        "to fetch the rest.\n\n"
        "Do NOT drill when the block is marked ``complete``: the "
        "search already showed you the entire body and drill will "
        "return the same text, wasting tokens and an LLM round-trip. "
        "Blocks without an explicit completeness qualifier (rare; "
        "legacy / lexical-only hits) are best-guess — drill only if "
        "the visible content seems truncated.\n\n"
        "Prefer the ``uris`` form whenever 2+ URIs from one search "
        "all need follow-up. Drill on URIs never expands the "
        "candidate set — use memory_search to find new candidates."
    ),
)


@tool_parameters(_PARAMETERS)
class MemoryDrillTool(Tool):
    """Drill one or many memory URIs in a single call."""

    config_key = "memory"

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def read_only(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "memory_drill"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        raw_uri = kwargs.get("uri")
        raw_uris = kwargs.get("uris")

        # Mutually exclusive surfaces. Empty / missing params produce a
        # clear error rather than a silent no-op.
        if raw_uri and raw_uris:
            return {
                "error": (
                    "pass either `uri` (single) or `uris` (list), not both"
                ),
            }
        if not raw_uri and not raw_uris:
            return {"error": "uri or uris is required"}

        if raw_uri:
            uri = str(raw_uri).strip()
            if not uri:
                return {"error": "uri is empty"}
            return self._drill_one(uri)

        if not isinstance(raw_uris, list):
            return {"error": "uris must be a list of strings"}
        if len(raw_uris) == 0:
            return {"error": "uris must be a non-empty list"}
        if len(raw_uris) > MAX_BATCH_URIS:
            return {
                "error": (
                    f"too many uris ({len(raw_uris)}); cap is "
                    f"{MAX_BATCH_URIS} per call — split the request "
                    f"or refine your memory_search first"
                ),
            }
        uris = [str(u).strip() for u in raw_uris]
        # Run drills concurrently — each is bound by a disk read, so
        # asyncio.to_thread parallelises naturally.
        results = await asyncio.gather(*[
            asyncio.to_thread(self._drill_one_safe, uri) for uri in uris
        ])
        return {"results": results}

    def _drill_one(self, uri: str) -> dict[str, Any]:
        """Single-URI drill — returns the legacy ``{uri, content}`` shape."""
        try:
            text = drill(self._workspace, uri)
        except DrillError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"io error: {exc}"}
        return {"uri": uri, "content": text}

    def _drill_one_safe(self, uri: str) -> dict[str, Any]:
        """Batch helper — never raises, always returns a record carrying
        the uri so the caller can match it back to its request."""
        if not uri:
            return {"uri": uri, "error": "empty uri"}
        try:
            text = drill(self._workspace, uri)
        except DrillError as exc:
            return {"uri": uri, "error": str(exc)}
        except OSError as exc:
            return {"uri": uri, "error": f"io error: {exc}"}
        return {"uri": uri, "content": text}
