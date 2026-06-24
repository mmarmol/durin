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
# Telemetry directory resolver (injectable for tests via monkeypatch)
# ---------------------------------------------------------------------------


def _telemetry_dir() -> Path:
    """Return the telemetry JSONL directory used by the logs reader."""
    return Path.home() / ".cache" / "durin" / "telemetry"

# ---------------------------------------------------------------------------
# Shared result — most graph-api calls return a plain dict
# ---------------------------------------------------------------------------


class MemoryResult(Result):
    """Carries a raw graph-api payload dict (escape hatch)."""

    data: dict[str, Any]


# ---------------------------------------------------------------------------
# Forget result — success only; failures are raised as DomainErrors
# ---------------------------------------------------------------------------


class ForgetResult(Result):
    """Result of a SUCCESSFUL archive — ``result`` is always ``"archived"``.

    The failure outcomes are raised as DomainErrors so the front door renders
    them as problem+json (one error format): protected → ``ForbiddenError`` (403),
    invalid → ``ValidationFailedError`` (422), not_found → ``NotFoundError`` (404).
    """

    result: str  # "archived"


# ---------------------------------------------------------------------------
# Dream digest DTOs
# ---------------------------------------------------------------------------


class DreamEvent(Result):
    """One notable thing the nightly dream did.

    ``kind`` is the operation: "merged" | "created" | "improved" | "flagged".
    ``ref`` / ``ref_kind`` let the UI deep-link to the affected entity or skill;
    both are None when the event is not tied to a specific ref (e.g. a bulk
    skill-extract pass with no individual ref emitted).  ``at_ms`` is epoch
    milliseconds so JS Date can consume it directly.
    """

    kind: str
    summary: str
    ref: str | None
    ref_kind: str | None  # "entity" | "skill" | None
    at_ms: int


class DreamDigest(Result):
    """List of recent dream events, newest first, capped at *limit*."""

    events: list[DreamEvent]
    last_run_at_ms: int | None  # timestamp of the most recent dream.end / dream.start


class DreamDigestQuery(Query):
    limit: int = 30


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
# Flagged-pairs DTOs (Dream Bandeja)
# ---------------------------------------------------------------------------


class FlaggedPair(Result):
    """One memory pair the dream escalated for human review."""

    ref_a: str
    ref_b: str
    verdict: str
    confidence: int
    reasoning: str
    at_ms: int | None


class FlaggedPairs(Result):
    """All memory pairs currently awaiting human review."""

    pairs: list[FlaggedPair]


class FlaggedPairsQuery(Query):
    """No inputs — returns the full current flagged-pairs list."""


class ResolveFlaggedRequest(Command):
    """Resolve a flagged pair: merge the two entities or keep them separate."""

    ref_a: str
    ref_b: str
    action: str  # "merge" | "separate"


class ResolveResult(Result):
    """Outcome of a resolve action."""

    ok: bool
    action: str


# ---------------------------------------------------------------------------
# Dream digest builder (module-level so it is easily unit-tested)
# ---------------------------------------------------------------------------

_DREAM_EVENT_TYPES = frozenset({
    "memory.absorb.auto_merged",
    "memory.dream.discover",
    "memory.dream.skill_extract",
    "memory.dream.skill_signals",
    "memory.dream.learnings",
    "memory.dream.flagged",
    "memory.dream.end",
    "memory.dream.start",
})

_RUN_MARKER_TYPES = frozenset({"memory.dream.end", "memory.dream.start"})


def _build_dream_digest(limit: int) -> DreamDigest:
    """Scan telemetry JSONL files and map dream events to DreamEvent objects.

    Reads from ``_telemetry_dir()`` using the same ``LogQuery`` / ``read_page``
    path the logs endpoint uses — no separate parsing logic.  Events are
    filtered by type, expanded (one raw event may produce multiple DreamEvents,
    e.g. ``dream.discover`` with several refs), and sorted newest-first before
    being capped at *limit*.

    ``last_run_at_ms`` uses the most recent ``memory.dream.end`` / ``dream.start``
    timestamp if present; falls back to the timestamp of the newest event.
    """
    from durin.logs.reader import LogQuery, read_page

    directory = _telemetry_dir()
    # Request enough raw events to fill *limit* after expansion; dream.discover
    # and dream.learnings can expand 1→N.  A 10× headroom is ample in practice.
    raw_limit = max(limit * 10, 300)
    log_query = LogQuery(
        source="telemetry",
        window_hours=None,  # unbounded so old dream runs are included
        limit=raw_limit,
    )
    try:
        page = read_page(directory, log_query)
    except Exception:  # noqa: BLE001
        return DreamDigest(events=[], last_run_at_ms=None)

    events: list[DreamEvent] = []
    last_run_at_ms: int | None = None

    # page.lines is newest-first (read_page reverses per-file)
    for line_dict in page.lines:
        raw = line_dict.get("raw", {})
        event_type: str = raw.get("type", "")
        if event_type not in _DREAM_EVENT_TYPES:
            continue

        ts_ms = int(float(raw.get("ts", 0)) * 1000)
        data: dict = raw.get("data") or {}

        if event_type in _RUN_MARKER_TYPES:
            if last_run_at_ms is None or ts_ms > last_run_at_ms:
                last_run_at_ms = ts_ms
            continue

        new_events = _map_event(event_type, data, ts_ms)
        events.extend(new_events)

    # Sort newest-first (page is already newest-first at the line level but
    # expansion can interleave timestamps from the same raw event).
    events.sort(key=lambda e: e.at_ms, reverse=True)
    events = events[:limit]

    if last_run_at_ms is None and events:
        last_run_at_ms = events[0].at_ms

    return DreamDigest(events=events, last_run_at_ms=last_run_at_ms)


def _map_event(event_type: str, data: dict, at_ms: int) -> list[DreamEvent]:
    """Map one raw telemetry event to zero or more DreamEvent objects."""

    if event_type == "memory.absorb.auto_merged":
        canonical = data.get("canonical", "")
        absorbed = data.get("absorbed", "")
        return [DreamEvent(
            kind="merged",
            summary=f"Merged {absorbed} → {canonical}",
            ref=canonical or None,
            ref_kind="entity" if canonical else None,
            at_ms=at_ms,
        )]

    if event_type == "memory.dream.discover":
        refs: list[str] = data.get("refs") or []
        written = data.get("written", len(refs))
        if not refs:
            return [DreamEvent(
                kind="created",
                summary=f"Discovered {written} entities",
                ref=None,
                ref_kind=None,
                at_ms=at_ms,
            )]
        return [
            DreamEvent(
                kind="created",
                summary=f"Discovered entity {ref}",
                ref=ref,
                ref_kind="entity",
                at_ms=at_ms,
            )
            for ref in refs
        ]

    if event_type == "memory.dream.learnings":
        refs = data.get("refs") or []
        written = data.get("written", len(refs))
        if not refs:
            return [DreamEvent(
                kind="created",
                summary=f"Logged {written} learnings",
                ref=None,
                ref_kind=None,
                at_ms=at_ms,
            )]
        return [
            DreamEvent(
                kind="created",
                summary=f"Logged learning {ref}",
                ref=ref,
                ref_kind="entity",
                at_ms=at_ms,
            )
            for ref in refs
        ]

    if event_type == "memory.dream.skill_extract":
        touched = data.get("skills_touched", 0)
        return [DreamEvent(
            kind="improved",
            summary=f"Updated {touched} skill(s) from session patterns",
            ref=None,
            ref_kind="skill",
            at_ms=at_ms,
        )]

    if event_type == "memory.dream.skill_signals":
        skills: list[str] = data.get("skills") or []
        logged = data.get("logged", len(skills))
        if not skills:
            return [DreamEvent(
                kind="improved",
                summary=f"Logged {logged} skill signal(s)",
                ref=None,
                ref_kind="skill",
                at_ms=at_ms,
            )]
        return [
            DreamEvent(
                kind="improved",
                summary=f"Logged skill signal for {skill}",
                ref=skill,
                ref_kind="skill",
                at_ms=at_ms,
            )
            for skill in skills
        ]

    if event_type == "memory.dream.flagged":
        canonical = data.get("canonical", "")
        absorbed = data.get("absorbed", "")
        return [DreamEvent(
            kind="flagged",
            summary="Flagged a memory pair for review",
            ref=canonical or None,
            ref_kind="entity" if canonical else None,
            at_ms=at_ms,
        )]

    return []


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

    @route(
        "GET",
        "/api/v1/memory/dream/digest",
        scope=Scope.MEMORY_READ.value,
        request_model=DreamDigestQuery,
        response_model=DreamDigest,
        summary="Recent dream-pass activity: merges, discoveries, skill updates",
    )
    async def dream_digest(
        self, query: DreamDigestQuery, principal: Principal
    ) -> DreamDigest:
        principal.require(Scope.MEMORY_READ)
        return _build_dream_digest(query.limit)

    @route(
        "GET",
        "/api/v1/memory/flagged-pairs",
        scope=Scope.MEMORY_READ.value,
        request_model=FlaggedPairsQuery,
        response_model=FlaggedPairs,
        summary="Memory pairs the dream flagged for human review (Dream Bandeja)",
    )
    async def flagged_pairs(
        self, query: FlaggedPairsQuery, principal: Principal
    ) -> FlaggedPairs:
        principal.require(Scope.MEMORY_READ)
        from datetime import datetime, timezone

        from durin.memory.refine_dream import read_flagged

        ws = self._workspace_resolver()
        raw = read_flagged(ws)
        pairs: list[FlaggedPair] = []
        for rec in raw:
            ref_a, ref_b = rec["pair"][0], rec["pair"][1]
            at_ms: int | None = None
            if "at" in rec:
                try:
                    dt = datetime.fromisoformat(rec["at"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    at_ms = int(dt.timestamp() * 1000)
                except Exception:
                    at_ms = None
            pairs.append(FlaggedPair(
                ref_a=ref_a,
                ref_b=ref_b,
                verdict=rec.get("verdict", ""),
                confidence=rec.get("confidence", 0),
                reasoning=rec.get("reasoning", ""),
                at_ms=at_ms,
            ))
        return FlaggedPairs(pairs=pairs)

    @route(
        "POST",
        "/api/v1/memory/flagged-pairs/resolve",
        scope=Scope.MEMORY_WRITE.value,
        request_model=ResolveFlaggedRequest,
        response_model=ResolveResult,
        summary="Resolve a flagged pair: merge the entities or keep them separate",
    )
    async def resolve_flagged(
        self, cmd: ResolveFlaggedRequest, principal: Principal
    ) -> ResolveResult:
        principal.require(Scope.MEMORY_WRITE)
        from durin.memory.refine_dream import add_tombstone, remove_flagged
        from durin.service.types import ValidationFailedError

        if cmd.action not in ("merge", "separate"):
            raise ValidationFailedError(
                f"unknown action: {cmd.action!r}; must be 'merge' or 'separate'",
                details={"action": cmd.action},
            )

        ws = self._workspace_resolver()

        if cmd.action == "merge":
            from durin.memory.absorption import AbsorptionError, EntityAbsorption
            from durin.service.types import ConflictError
            try:
                EntityAbsorption(workspace=ws).absorb(
                    cmd.ref_a, cmd.ref_b, reason="manual_review",
                )
            except AbsorptionError as exc:
                raise ConflictError(
                    f"could not merge: {exc}",
                    details={"ref_a": cmd.ref_a, "ref_b": cmd.ref_b},
                ) from exc
        else:
            add_tombstone(ws, cmd.ref_a, cmd.ref_b)

        remove_flagged(ws, cmd.ref_a, cmd.ref_b)
        return ResolveResult(ok=True, action=cmd.action)

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
        from durin.service.types import (
            ForbiddenError,
            NotFoundError,
            ValidationFailedError,
        )

        ws = self._workspace_resolver()
        result = forget_entry(ws, cmd.uri).get("result", "invalid")
        if result == "archived":
            return ForgetResult(result=result)
        # Failure outcomes → one problem+json error format (result echoed in details).
        details = {"result": result, "uri": cmd.uri}
        if result == "protected":
            raise ForbiddenError("memory entry is protected", details=details)
        if result == "not_found":
            raise NotFoundError("memory entry not found", details=details)
        raise ValidationFailedError("invalid memory uri", details=details)
