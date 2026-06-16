"""MemoryService — transport-agnostic wrapper over ``durin.memory.graph_api``.

Every service method maps 1:1 to a ``_handle_memory_*`` handler in
``durin/channels/websocket.py``. The handler bodies delegate to graph-api
functions; this service lifts that delegation out so the shim becomes a thin
auth + parse + serialize wrapper.

Result shape
------------
Graph-API payloads are large/dynamic dicts.  All read methods share a single
``MemoryResult`` with ``data: dict[str, Any]`` (escape hatch — open by design).
``ForgetResult`` is kept separate because its ``result`` field drives HTTP status
selection in the shim.

Workspace dependency
--------------------
The service is constructed with a ``workspace_resolver`` callable (typically
``self._endpoint_workspace`` from the channel) so the workspace is re-evaluated
per call — unlike ``SkillsService`` which captures the workspace at build time.
For the search endpoint the embedding model is resolved from ``load_config()``
at call time (same logic as the legacy handler).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, Query, Result

# ---------------------------------------------------------------------------
# Shared result — most graph-api calls return a plain dict
# ---------------------------------------------------------------------------


class MemoryResult(Result):
    """Carries a raw graph-api payload dict (escape hatch)."""

    data: dict[str, Any]


# ---------------------------------------------------------------------------
# Forget result — status selection depends on the result field
# ---------------------------------------------------------------------------


class ForgetResult(Result):
    """Result of an archive operation.  ``result`` drives the HTTP ``status``
    (200 archived/not_found, 403 protected, 400 invalid)."""

    result: str  # "archived" | "not_found" | "protected" | "invalid"
    status: int = 200


# ---------------------------------------------------------------------------
# Read DTOs
# ---------------------------------------------------------------------------


class MemoryGraphQuery(Query):
    """No inputs — returns the full entity graph."""


class MemorySubgraphQuery(Query):
    ref: str
    hops: int = 1


class MemoryEntityQuery(Query):
    ref: str  # URL-decoded entity reference e.g. "person:marcelo"


class MemorySessionQuery(Query):
    stem: str  # URL-decoded session stem e.g. "cli_direct"


class MemoryEntryQuery(Query):
    uri: str


class MemoryBacklinksQuery(Query):
    uri: str


class MemoryEdgeQuery(Query):
    a: str  # source ref (URL-decoded)
    b: str  # target ref (URL-decoded)


class MemorySearchQuery(Query):
    q: str
    scope: str = "all"
    level: str = "warm"
    kinds: str = "all"


# ---------------------------------------------------------------------------
# Write DTOs
# ---------------------------------------------------------------------------


class MemoryForgetCommand(Command):
    uri: str


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MemoryService:
    """Delegates all calls to ``durin.memory.graph_api`` after checking scope.

    ``workspace_resolver`` is a zero-argument callable that returns the active
    workspace ``Path``.  The channel injects ``self._endpoint_workspace`` so
    session-manager overrides are honoured per-call.
    """

    def __init__(self, workspace_resolver: Callable[[], Path]) -> None:
        self._workspace_resolver = workspace_resolver

    # -- reads ---------------------------------------------------------------

    @route(
        "GET",
        "/api/v1/memory/graph",
        scope=Scope.MEMORY_READ.value,
        request_model=MemoryGraphQuery,
        response_model=MemoryResult,
        summary="Entity-centric memory as nodes + edges (graph view)",
    )
    async def graph(
        self, query: MemoryGraphQuery, principal: Principal
    ) -> MemoryResult:
        principal.require(Scope.MEMORY_READ)
        from durin.memory.graph import build_memory_graph

        ws = self._workspace_resolver()
        payload = build_memory_graph(ws)
        return MemoryResult(data=payload)

    @route(
        "GET",
        "/api/v1/memory/subgraph",
        scope=Scope.MEMORY_READ.value,
        request_model=MemorySubgraphQuery,
        response_model=MemoryResult,
        summary="Ego-graph: a node + its N-hop neighbourhood",
    )
    async def subgraph(
        self, query: MemorySubgraphQuery, principal: Principal
    ) -> MemoryResult:
        principal.require(Scope.MEMORY_READ)
        from durin.memory.graph import build_entity_subgraph

        ws = self._workspace_resolver()
        payload = build_entity_subgraph(ws, query.ref, hops=max(1, min(query.hops, 3)))
        return MemoryResult(data=payload)

    @route(
        "GET",
        "/api/v1/memory/entity/{ref}",
        scope=Scope.MEMORY_READ.value,
        request_model=MemoryEntityQuery,
        response_model=MemoryResult,
        summary="Full entity page + history + archive + entries",
    )
    async def entity(
        self, query: MemoryEntityQuery, principal: Principal
    ) -> MemoryResult:
        principal.require(Scope.MEMORY_READ)
        from durin.memory.graph_api import get_entity_detail
        from durin.service.types import NotFoundError

        ws = self._workspace_resolver()
        payload = get_entity_detail(ws, query.ref)
        if payload is None:
            raise NotFoundError(f"entity not found: {query.ref}")
        return MemoryResult(data=payload)

    @route(
        "GET",
        "/api/v1/memory/session/{stem}",
        scope=Scope.MEMORY_READ.value,
        request_model=MemorySessionQuery,
        response_model=MemoryResult,
        summary="Session detail for the graph view",
    )
    async def session(
        self, query: MemorySessionQuery, principal: Principal
    ) -> MemoryResult:
        principal.require(Scope.MEMORY_READ)
        from durin.memory.graph_api import get_session_detail
        from durin.service.types import NotFoundError

        ws = self._workspace_resolver()
        payload = get_session_detail(ws, query.stem)
        if payload is None:
            raise NotFoundError(f"session not found: {query.stem}")
        return MemoryResult(data=payload)

    @route(
        "GET",
        "/api/v1/memory/entry",
        scope=Scope.MEMORY_READ.value,
        request_model=MemoryEntryQuery,
        response_model=MemoryResult,
        summary="One entry's frontmatter + body",
    )
    async def entry(
        self, query: MemoryEntryQuery, principal: Principal
    ) -> MemoryResult:
        principal.require(Scope.MEMORY_READ)
        from durin.memory.graph_api import get_entry_detail
        from durin.service.types import NotFoundError

        ws = self._workspace_resolver()
        payload = get_entry_detail(ws, query.uri)
        if payload is None:
            raise NotFoundError(f"entry not found: {query.uri}")
        return MemoryResult(data=payload)

    @route(
        "GET",
        "/api/v1/memory/backlinks",
        scope=Scope.MEMORY_READ.value,
        request_model=MemoryBacklinksQuery,
        response_model=MemoryResult,
        summary="Entries that reference the given URI",
    )
    async def backlinks(
        self, query: MemoryBacklinksQuery, principal: Principal
    ) -> MemoryResult:
        principal.require(Scope.MEMORY_READ)
        from durin.memory.graph_api import get_entry_backlinks

        ws = self._workspace_resolver()
        payload = get_entry_backlinks(ws, query.uri)
        return MemoryResult(data=payload)

    @route(
        "GET",
        "/api/v1/memory/edge/{a}/{b}",
        scope=Scope.MEMORY_READ.value,
        request_model=MemoryEdgeQuery,
        response_model=MemoryResult,
        summary="Entries co-mentioning both refs (edge evidence)",
    )
    async def edge(
        self, query: MemoryEdgeQuery, principal: Principal
    ) -> MemoryResult:
        principal.require(Scope.MEMORY_READ)
        from durin.memory.graph_api import get_edge_detail

        ws = self._workspace_resolver()
        payload = get_edge_detail(ws, query.a, query.b)
        return MemoryResult(data=payload)

    @route(
        "GET",
        "/api/v1/memory/search",
        scope=Scope.MEMORY_READ.value,
        request_model=MemorySearchQuery,
        response_model=MemoryResult,
        summary="Memory search — same shape as the memory_search LLM tool",
    )
    async def search(
        self, query: MemorySearchQuery, principal: Principal
    ) -> MemoryResult:
        principal.require(Scope.MEMORY_READ)
        from durin.config.loader import load_config
        from durin.memory.graph_api import search_memory_api

        cfg = load_config()
        workspace = cfg.workspace_path
        embedding_model = None
        try:
            if cfg.memory.enabled:
                embedding_model = cfg.memory.embedding.model
        except (AttributeError, TypeError):
            embedding_model = None
        payload = await search_memory_api(
            workspace,
            query.q,
            scope=query.scope,
            level=query.level,
            kinds=query.kinds,
            embedding_model=embedding_model,
        )
        return MemoryResult(data=payload)

    # -- writes --------------------------------------------------------------

    @route(
        "DELETE",
        "/api/v1/memory/entry",
        scope=Scope.MEMORY_WRITE.value,
        request_model=MemoryForgetCommand,
        response_model=ForgetResult,
        summary="Archive a memory entry",
    )
    async def forget(
        self, cmd: MemoryForgetCommand, principal: Principal
    ) -> ForgetResult:
        principal.require(Scope.MEMORY_WRITE)
        from durin.memory.graph_api import forget_entry

        ws = self._workspace_resolver()
        payload = forget_entry(ws, cmd.uri)
        result = payload.get("result", "invalid")
        status = {"protected": 403, "invalid": 400}.get(result, 200)
        return ForgetResult(result=result, status=status)
