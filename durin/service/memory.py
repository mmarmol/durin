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
    from durin.config.paths import get_telemetry_dir

    return get_telemetry_dir()

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

    ``kind`` is the operation: "merged" | "resolved" (the dream disambiguated
    or related a colliding pair on its own) | "created" | "improved" | "flagged" |
    "warning" (degraded/unparseable — needs operator attention) | "run"
    (per-run summary line).
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


class DreamLastRun(Result):
    """Counts for the most recent dream run — drives the "última corrida" card.

    These are headline pass deltas for that one run (per-item detail is in the
    events feed). All-zero is a valid, expected state: an idle run that found
    nothing new still reports 0/0/0/0 so the card always shows what the last run
    did rather than going blank.
    """

    at_ms: int
    sessions: int
    entities: int
    merged: int
    skills_created: int
    skills_improved: int


class DreamDigest(Result):
    """Recent dream activity: the latest run's counts (headline) plus a
    newest-first feed of prior runs and their activity, capped at *limit*."""

    events: list[DreamEvent]
    last_run: DreamLastRun | None  # the most recent run's counts (headline card)
    last_run_at_ms: int | None  # timestamp of the most recent dream.end / dream.start


class DreamDigestQuery(Query):
    limit: int = 30


# ---------------------------------------------------------------------------
# Read DTOs
# ---------------------------------------------------------------------------


class MemoryGraphQuery(Query):
    """No inputs — returns the full entity graph."""


class MemorySubgraphQuery(Query):
    """Ego neighbourhood around a ref: the node plus everything within
    ``hops`` edges (server-clamped to 1–3)."""

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
    keywords: str = ""


class MemoryDocumentsQuery(Query):
    """No inputs — lists all ingested reference documents (the Library shelf)."""


class MemoryDocumentQuery(Query):
    slug: str  # URL-decoded reference slug e.g. "the-durin-handbook"


# ---------------------------------------------------------------------------
# Write DTOs
# ---------------------------------------------------------------------------


class MemoryForgetCommand(Command):
    uri: str


class MemoryDocumentForgetCommand(Command):
    slug: str  # reference slug from the path, e.g. "the-durin-handbook"


# ---------------------------------------------------------------------------
# Flagged-pairs DTOs (Dream Bandeja)
# ---------------------------------------------------------------------------


class FlaggedPair(Result):
    """One memory pair the dream escalated for human review.

    ``name_*`` / ``aliases_*`` are the pages' current display name and
    aliases (empty when a page no longer exists), so the Bandeja can offer
    alias ownership without a round-trip per page. ``proposal`` is the
    judge's resolution (see ``durin.memory.pair_resolution``) — ``kind``
    merge | disambiguate | relate | keep, plus ``survivor``, ``renames``,
    ``alias_moves`` and ``relation`` — or None when it proposed none.
    ``source`` says who flagged it: tier1 | tier2 | rereview."""

    ref_a: str
    ref_b: str
    verdict: str
    confidence: int
    reasoning: str
    at_ms: int | None
    name_a: str = ""
    name_b: str = ""
    aliases_a: list[str] = []
    aliases_b: list[str] = []
    proposal: dict[str, Any] | None = None
    source: str | None = None


class FlaggedPairs(Result):
    """All memory pairs currently awaiting human review."""

    pairs: list[FlaggedPair]


class FlaggedPairsQuery(Query):
    """No inputs — returns the full current flagged-pairs list."""


class AliasMoveIn(Command):
    """Who keeps ``alias``: ``ref_a``/``ref_b`` (either ref), ``both`` or ``none``."""

    alias: str
    keep_on: str


class RelationIn(Command):
    """A typed edge between the two pages of the pair."""

    from_ref: str
    type: str
    to_ref: str


class RenameIn(Command):
    """A clearer key (``slug``) and/or display ``name`` for one page."""

    slug: str | None = None
    name: str | None = None


class ResolveFlaggedRequest(Command):
    """Resolve a flagged pair.

    ``action``:
    - ``merge`` — fold the pair into ``survivor`` (default ``ref_a``);
      ``renames`` may give the survivor a clearer key.
    - ``separate`` — keep both as they are (tombstones the pair).
    - ``disambiguate`` — keep both, apply ``alias_moves`` / ``renames``.
    - ``relate`` — ``disambiguate`` plus ``relation``.
    - ``accept`` — apply the judge's stored proposal as is.
    Every action except ``merge`` tombstones the pair (under the final keys),
    so the dream never merges what the user kept apart."""

    ref_a: str
    ref_b: str
    action: str
    survivor: str | None = None
    renames: dict[str, RenameIn] | None = None
    alias_moves: list[AliasMoveIn] | None = None
    relation: RelationIn | None = None


class ResolveResult(Result):
    """Outcome of a resolve action. ``refs`` maps each original ref to the
    ref it has now (a merged-away page maps to the survivor; a renamed page
    to its new key)."""

    ok: bool
    action: str
    refs: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Dream digest builder (module-level so it is easily unit-tested)
#
# The event-type sets and the event→item mapping live in
# ``durin.memory.dream_digest`` so the live websocket tee renders items
# identical to this after-the-fact digest.
# ---------------------------------------------------------------------------


def _build_dream_digest(workspace: Path, limit: int) -> DreamDigest:
    """Assemble the dream digest: the run summaries (the "última corrida" card +
    run history) come from the DURABLE run store, and the fine-grained activity
    feed (merges, discoveries, skill-curation actions) from telemetry.

    Run summaries used to be re-derived from telemetry, which is capped and
    retention-deleted — a busy refine pass flooded the window and the summary
    silently vanished. The durable ``read_dream_runs`` store fixes that; telemetry
    remains the source for the recent per-item activity only.
    """
    from durin.logs.reader import LogQuery, read_page
    from durin.memory.dream_digest import (
        DREAM_EVENT_TYPES,
        RUN_MARKER_TYPES,
        map_dream_event,
    )
    from durin.memory.dream_runs import read_dream_runs

    runs = read_dream_runs(workspace, limit=max(limit, 20))

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
        return DreamDigest(events=[], last_run=None, last_run_at_ms=None)

    events: list[DreamEvent] = []
    last_run_at_ms: int | None = None
    # The "última corrida" card + run history come from the durable store.
    last_run: DreamLastRun | None = None
    if runs:
        r0 = runs[0]
        last_run = DreamLastRun(
            at_ms=int(r0.get("at_ms", 0)),
            sessions=int(r0.get("sessions", 0)),
            entities=int(r0.get("entities", 0)),
            merged=int(r0.get("merged", 0)),
            skills_created=int(r0.get("skills_created", 0)),
            skills_improved=int(r0.get("skills_improved", 0)),
        )
        # Older runs become "run" history entries in the feed (durable, never
        # window-truncated), rendered by the same mapping the live tee uses.
        for r in runs[1:]:
            for d in map_dream_event("memory.dream.run_summary", r, int(r.get("at_ms", 0))):
                events.append(DreamEvent(**d))

    # page.lines is newest-first (read_page reverses per-file). Telemetry supplies
    # the per-item ACTIVITY only; run summaries are owned by the durable store above.
    for line_dict in page.lines:
        raw = line_dict.get("raw", {})
        event_type: str = raw.get("type", "")
        if event_type not in DREAM_EVENT_TYPES:
            continue

        ts_ms = int(float(raw.get("ts", 0)) * 1000)
        data: dict = raw.get("data") or {}

        if event_type in RUN_MARKER_TYPES:
            if last_run_at_ms is None or ts_ms > last_run_at_ms:
                last_run_at_ms = ts_ms
            continue

        if event_type == "memory.dream.run_summary":
            continue  # durable store owns run summaries

        new_events = [DreamEvent(**d) for d in map_dream_event(event_type, data, ts_ms)]
        events.extend(new_events)

    # Sort newest-first (page is already newest-first at the line level but
    # expansion can interleave timestamps from the same raw event).
    events.sort(key=lambda e: e.at_ms, reverse=True)
    events = events[:limit]

    if last_run_at_ms is None:
        last_run_at_ms = last_run.at_ms if last_run else (events[0].at_ms if events else None)

    return DreamDigest(events=events, last_run=last_run, last_run_at_ms=last_run_at_ms)


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
        summary="Entity-centric memory as nodes + edges (entity browser)",
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
        import asyncio

        from durin.memory.graph import build_entity_subgraph, get_full_graph_cached

        ws = self._workspace_resolver()

        def _ego() -> dict[str, Any]:
            full = get_full_graph_cached(ws)
            return build_entity_subgraph(
                ws, query.ref, hops=max(1, min(query.hops, 3)), payload=full
            )

        payload = await asyncio.to_thread(_ego)
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
        "/api/v1/memory/documents",
        scope=Scope.MEMORY_READ.value,
        request_model=MemoryDocumentsQuery,
        response_model=MemoryResult,
        summary="List ingested reference documents (the Library shelf)",
    )
    async def documents(
        self, query: MemoryDocumentsQuery, principal: Principal
    ) -> MemoryResult:
        principal.require(Scope.MEMORY_READ)
        from durin.memory.graph_api import list_reference_documents

        ws = self._workspace_resolver()
        return MemoryResult(data={"documents": list_reference_documents(ws)})

    @route(
        "GET",
        "/api/v1/memory/documents/{slug}",
        scope=Scope.MEMORY_READ.value,
        request_model=MemoryDocumentQuery,
        response_model=MemoryResult,
        summary="Reference document detail: outline + derived entities + chunk preview",
    )
    async def document(
        self, query: MemoryDocumentQuery, principal: Principal
    ) -> MemoryResult:
        principal.require(Scope.MEMORY_READ)
        from durin.memory.graph_api import get_reference_detail
        from durin.service.types import NotFoundError

        ws = self._workspace_resolver()
        payload = get_reference_detail(ws, query.slug)
        if payload is None:
            raise NotFoundError(f"reference not found: {query.slug}")
        return MemoryResult(data=payload)

    @route(
        "DELETE",
        "/api/v1/memory/documents/{slug}",
        scope=Scope.MEMORY_WRITE.value,
        request_model=MemoryDocumentForgetCommand,
        response_model=ForgetResult,
        summary="Forget an ingested reference document (archive + drop index rows)",
    )
    async def forget_document(
        self, cmd: MemoryDocumentForgetCommand, principal: Principal
    ) -> ForgetResult:
        principal.require(Scope.MEMORY_WRITE)
        from durin.memory.graph_api import forget_reference
        from durin.service.types import NotFoundError, ValidationFailedError

        ws = self._workspace_resolver()
        result = forget_reference(ws, cmd.slug).get("result", "error")
        if result == "archived":
            return ForgetResult(result=result)
        details = {"result": result, "slug": cmd.slug}
        if result == "not_found":
            raise NotFoundError("reference document not found", details=details)
        raise ValidationFailedError("could not forget document", details=details)

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
            keywords=query.keywords,
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
        return _build_dream_digest(self._workspace_resolver(), query.limit)

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

        def _page_info(ref: str) -> tuple[str, list[str]]:
            from durin.memory.entity_page import EntityPage
            t, _, s = ref.partition(":")
            path = Path(ws) / "memory" / "entities" / t / f"{s}.md"
            try:
                page = EntityPage.from_file(path) if path.exists() else None
            except OSError:
                page = None
            return (page.name, list(page.aliases or [])) if page else ("", [])

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
            name_a, aliases_a = _page_info(ref_a)
            name_b, aliases_b = _page_info(ref_b)
            proposal = rec.get("proposal")
            pairs.append(FlaggedPair(
                ref_a=ref_a,
                ref_b=ref_b,
                verdict=rec.get("verdict", ""),
                confidence=rec.get("confidence", 0),
                reasoning=rec.get("reasoning", ""),
                at_ms=at_ms,
                name_a=name_a,
                name_b=name_b,
                aliases_a=aliases_a,
                aliases_b=aliases_b,
                proposal=proposal if isinstance(proposal, dict) else None,
                source=rec.get("source"),
            ))
        return FlaggedPairs(pairs=pairs)

    @route(
        "POST",
        "/api/v1/memory/flagged-pairs/resolve",
        scope=Scope.MEMORY_WRITE.value,
        request_model=ResolveFlaggedRequest,
        response_model=ResolveResult,
        summary="Resolve a flagged pair: merge, keep separate, disambiguate, relate, or accept the proposal",
    )
    async def resolve_flagged(
        self, cmd: ResolveFlaggedRequest, principal: Principal
    ) -> ResolveResult:
        principal.require(Scope.MEMORY_WRITE)
        from durin.memory.absorption import AbsorptionError
        from durin.memory.entity_rename import EntityRenameError
        from durin.memory.memory_writer import StaleContentError
        from durin.memory.pair_resolution import (
            AliasMove,
            PairPageMissingError,
            RelationSpec,
            RenameSpec,
            Resolution,
            ResolutionError,
            apply_resolution,
        )
        from durin.memory.refine_dream import add_tombstone, read_flagged, remove_flagged
        from durin.service.types import ConflictError, ValidationFailedError

        actions = ("merge", "separate", "disambiguate", "relate", "accept")
        if cmd.action not in actions:
            raise ValidationFailedError(
                f"unknown action: {cmd.action!r}; must be one of {', '.join(actions)}",
                details={"action": cmd.action},
            )

        ws = self._workspace_resolver()
        details = {"ref_a": cmd.ref_a, "ref_b": cmd.ref_b}

        # "Keep separate" with nothing to change stays a pure tombstone, so it
        # works even when one of the pages has since disappeared.
        if cmd.action == "separate" and not (cmd.renames or cmd.alias_moves):
            add_tombstone(ws, cmd.ref_a, cmd.ref_b)
            remove_flagged(ws, cmd.ref_a, cmd.ref_b)
            return ResolveResult(ok=True, action=cmd.action,
                                 refs={cmd.ref_a: cmd.ref_a, cmd.ref_b: cmd.ref_b})

        if cmd.action == "accept":
            key = sorted([cmd.ref_a, cmd.ref_b])
            rec = next((r for r in read_flagged(ws) if sorted(r.get("pair") or []) == key), None)
            if not rec or not isinstance(rec.get("proposal"), dict):
                raise ValidationFailedError("this pair has no proposal to accept",
                                            details=details)
            try:
                res = Resolution.from_dict(rec["proposal"])
            except ResolutionError as exc:
                raise ValidationFailedError(f"stored proposal is unusable: {exc}",
                                            details=details) from exc
            res.source = res.source or "bandeja"
        else:
            kind = {"separate": "keep"}.get(cmd.action, cmd.action)
            res = Resolution(
                kind=kind,
                survivor=cmd.survivor,
                renames={ref: RenameSpec(slug=r.slug, name=r.name)
                         for ref, r in (cmd.renames or {}).items()},
                alias_moves=[AliasMove(alias=m.alias, keep_on=m.keep_on)
                             for m in (cmd.alias_moves or [])],
                relation=(RelationSpec(from_ref=cmd.relation.from_ref, type=cmd.relation.type,
                                       to_ref=cmd.relation.to_ref)
                          if cmd.relation else None),
                source="bandeja",
            )
            if kind == "keep" and res.changes_anything():
                res.kind = "disambiguate"

        try:
            out = apply_resolution(ws, res, cmd.ref_a, cmd.ref_b, actor="user")
        except PairPageMissingError as exc:
            # Stale pair (a page was merged, renamed or deleted since it was
            # flagged): a conflict with the current state, not a bad request.
            raise ConflictError(str(exc), details=details) from exc
        except (ResolutionError, EntityRenameError) as exc:
            raise ValidationFailedError(str(exc), details=details) from exc
        except AbsorptionError as exc:
            raise ConflictError(f"could not merge: {exc}", details=details) from exc
        except StaleContentError as exc:
            # Memory kept changing under the resolution (a busy dream run):
            # nothing was written; retrying later is safe.
            raise ConflictError(f"memory changed while resolving: {exc}",
                                details=details) from exc
        return ResolveResult(ok=True, action=cmd.action, refs=out.refs)

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
