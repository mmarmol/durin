"""mcp_search tool — search the MCP registry for an installable server.

Auto-discovered into the agent's `core` toolset (like ``skill_search``). Read-only:
returns ranked hits across the configured registries. To bring one in, the agent
pipes its ``ref`` through the gated ``mcp_manage`` tool — search NEVER installs.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from durin.agent.mcp_catalog_cache import McpCatalogCache
from durin.agent.mcp_registry import build_mcp_adapters, search_mcp_registries
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    query=StringSchema(
        "What MCP capability to search for (e.g. 'jira', 'postgres', 'github')."
    ),
    limit=IntegerSchema(
        description="Max results to return (default from config).", minimum=1
    ),
    include_all=BooleanSchema(
        description="Include community/unverified servers (bypass the stars filter). Default false."
    ),
    description=(
        "Search the MCP registry for an installable server. Returns ranked hits, "
        "each with a 'ref'. To add one, call mcp_manage(action='install', ref=<ref>) "
        "— search never installs."
    ),
)


@tool_parameters(_PARAMETERS)
class McpSearchTool(Tool):
    """mcp_search tool — unified search over MCP registries."""

    def __init__(self, cache_path, registries=None, limit: int = 10, quality="official", min_stars=100) -> None:
        self._cache_path = Path(cache_path)
        self._registries = list(registries or [])
        self._limit = int(limit or 10)
        self._quality = quality
        self._min_stars = min_stars

    @property
    def name(self) -> str:
        return "mcp_search"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> "McpSearchTool":
        from durin.config.loader import get_config_path

        try:
            disc = ctx.app_config.tools.mcp_discovery
        except Exception:  # noqa: BLE001
            from durin.config.loader import load_config

            disc = load_config().tools.mcp_discovery
        return cls(
            cache_path=get_config_path().parent / "mcp_catalog.json",
            registries=list(disc.registries),
            limit=int(disc.search_limit),
            quality=disc.quality,
            min_stars=disc.min_stars,
        )

    async def execute(self, **kwargs: Any) -> Any:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return {"error": "query is required"}
        limit = int(kwargs.get("limit") or self._limit)
        include_all = bool(kwargs.get("include_all"))
        quality = "all" if include_all else self._quality
        hits = await search_mcp_registries(
            query,
            cache=McpCatalogCache(self._cache_path),
            adapters=build_mcp_adapters(self._registries),
            limit=limit,
            quality=quality,
            min_stars=self._min_stars,
        )
        return {
            "hits": [asdict(h) for h in hits],
            "note": (
                "to add a hit, call mcp_manage(action='install', ref=<ref>); "
                "search never installs"
            ),
        }
