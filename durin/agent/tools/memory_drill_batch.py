"""memory_drill_batch tool — drill N URIs in a single call (audit H6, 2026-05-29).

Bench v4 (post H4+H5) confirmed the drill ratio dropped 42% once the
LLM had the `complete` / `preview N/M` marker. But when the agent
DOES need multiple drills (cross-referencing several `preview` blocks
to answer one question), it still fires one ``memory_drill`` per
uri — N tool calls, N LLM round-trips, N agent iterations.

H6 ships a batch variant: pass a list of URIs, get back a list of
``{uri, content, error}`` records in one call. Same payload, single
round-trip, lower latency.

The single-uri ``memory_drill`` tool stays — the batch variant is
purely additive. Use single when you're sure exactly one body
matters; use batch when 2+ candidates emerged from the same search.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import (
    ArraySchema, StringSchema, tool_parameters_schema,
)
from durin.memory.drill import DrillError, drill

# Cap on the number of URIs per batch call. 10 chosen because
# ``memory_search.limit`` defaults to 10 — drilling more than the
# default search returns is almost always a sign the agent should
# narrow the query instead of dumping bodies into context.
MAX_BATCH_URIS: int = 10

_PARAMETERS = tool_parameters_schema(
    uris=ArraySchema(
        items=StringSchema(
            "Markdown URI such as 'sessions/<key>.md#turn-42', "
            "'ingested/<id>/source.md#section-3', or "
            "'memory/<class>/<id>'."
        ),
        description=(
            f"List of URIs to drill in a single call. "
            f"Maximum {MAX_BATCH_URIS} per batch. "
            "Results come back in the same order."
        ),
    ),
    required=["uris"],
    description=(
        # Canonical text per `docs/memory/06_prompts_and_instructions.md` §3.5.
        "Read the full content of multiple memory items by URI in a "
        "single tool call.\n\n"
        f"Pass up to {MAX_BATCH_URIS} URIs; the response carries one "
        "``{uri, content}`` record per request in the same order, plus "
        "an ``error`` field on entries that failed (missing file, "
        "malformed uri, etc.). Failing one uri does NOT abort the "
        "others.\n\n"
        "Use this INSTEAD of N back-to-back ``memory_drill`` calls "
        "when a single memory_search result block flagged multiple "
        "URIs as ``preview N/M`` and you need to cross-reference them. "
        "One round-trip, lower latency, same payload as N drills.\n\n"
        "Do NOT batch when the search marked the relevant blocks as "
        "``complete``: drill returns the same text already shown. "
        "Do NOT use as a replacement for memory_search — drill on N "
        "URIs never expands the candidate set."
    ),
)


@tool_parameters(_PARAMETERS)
class MemoryDrillBatchTool(Tool):
    """Drill multiple memory URIs in one tool call."""

    config_key = "memory"

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def read_only(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "memory_drill_batch"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        raw = kwargs.get("uris")
        if not isinstance(raw, list) or not raw:
            return {"error": "uris must be a non-empty list of strings"}
        if len(raw) > MAX_BATCH_URIS:
            return {
                "error": (
                    f"too many uris ({len(raw)}); cap is "
                    f"{MAX_BATCH_URIS} per batch — split the request "
                    f"or refine your memory_search first"
                ),
            }
        uris = [str(u).strip() for u in raw]
        # Run drills concurrently — each is bound by a disk read, so
        # asyncio.to_thread parallelises naturally without bumping
        # against a global event-loop bottleneck.
        results = await asyncio.gather(*[
            asyncio.to_thread(self._safe_drill_one, uri) for uri in uris
        ])
        return {"results": results}

    def _safe_drill_one(self, uri: str) -> dict[str, Any]:
        if not uri:
            return {"uri": uri, "error": "empty uri"}
        try:
            text = drill(self._workspace, uri)
        except DrillError as exc:
            return {"uri": uri, "error": str(exc)}
        except OSError as exc:
            return {"uri": uri, "error": f"io error: {exc}"}
        return {"uri": uri, "content": text}
