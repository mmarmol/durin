"""skill_search tool — search external skill registries for a skill.

Auto-discovered into the agent's `core` toolset (like skill_import / skill_audit).
Search-only: returns ranked hits across the configured registries. To bring a hit
in, the agent pipes its `ref` through the gated skill_import tool — search NEVER
installs.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    query=StringSchema(
        "What to search for across the configured skill registries "
        "(e.g. 'pdf', 'web scraping')."
    ),
    limit=IntegerSchema(
        description="Max results to return (default from config).",
        minimum=1,
    ),
    description=(
        "Search external skill registries (skills.sh, …) for a skill. Returns "
        "ranked hits, each with a 'ref'. To install one, pass its ref to "
        "skill_import(action='fetch', source=<ref>) — search never installs."
    ),
)


@tool_parameters(_PARAMETERS)
class SkillSearchTool(Tool):
    """skill_search tool — unified search over skill registries."""

    def __init__(self, workspace: str | Path, registries: list | None = None,
                 allowlist: list[str] | None = None, limit: int = 10) -> None:
        self._workspace = Path(workspace).expanduser()
        self._registries = list(registries or [])
        self._allowlist = list(allowlist or [])
        self._limit = int(limit or 10)

    @property
    def name(self) -> str:
        return "skill_search"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> "SkillSearchTool":
        registries: list = []
        allowlist: list[str] = []
        limit = 10
        try:
            sk = ctx.app_config.skills
        except Exception:  # noqa: BLE001
            try:
                from durin.config.loader import load_config
                sk = load_config().skills
            except Exception:  # noqa: BLE001
                sk = None
        if sk is not None:
            registries = list(sk.discovery.registries)
            limit = int(sk.discovery.search_limit)
            allowlist = list(sk.security.allowlist)
        return cls(workspace=ctx.workspace, registries=registries,
                   allowlist=allowlist, limit=limit)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skill_registry import build_adapters, search_registries

        query = str(kwargs.get("query", "")).strip()
        if not query:
            return {"error": "query is required"}
        limit = int(kwargs.get("limit") or self._limit)
        hits = await search_registries(
            query, adapters=build_adapters(self._registries),
            allowlist=self._allowlist, limit=limit)
        return {
            "hits": [{"name": h.name, "ref": h.ref, "registry": h.registry,
                      "description": h.description, "signals": h.signals} for h in hits],
            "note": "to import a hit, call skill_import(action='fetch', source=<ref>); search never installs",
        }
