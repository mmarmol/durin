"""memory_search tool — Phase-1 grep over memory entries and session views."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import logging
import time
from typing import Optional

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_page import EntityPage
from durin.memory.entity_ranker import (
    extract_query_entities,
    rank_with_entities,
)
from durin.memory.search import Result, search_memory
from durin.memory.vector_index import VectorIndex, vector_index_available

logger = logging.getLogger(__name__)


def _load_cursors_from_entities_dir(
    memory_root: Path,
    entity_refs: list[str],
) -> dict[str, Any]:
    """Read ``dream_processed_through`` from each entity's page (S3, doc 24).

    Returns ``{entity_ref: cursor_value}`` for refs whose page exists and
    has a cursor field. Used by entity_ranker to apply the pre/post-cursor
    boost/demote. Best-effort — missing or unparseable pages skip silently.
    """
    cursors: dict[str, Any] = {}
    for ref in entity_refs:
        if ":" not in ref:
            continue
        type_, slug = ref.split(":", 1)
        page_path = memory_root / "entities" / type_ / f"{slug}.md"
        if not page_path.exists():
            continue
        try:
            page = EntityPage.from_file(page_path)
        except Exception:  # noqa: BLE001
            continue
        if page is not None and page.dream_processed_through is not None:
            cursors[ref] = page.dream_processed_through
    return cursors

_PARAMETERS = tool_parameters_schema(
    query=StringSchema(
        "What to look for. Use a short topical phrase or natural question "
        "(2-6 words, e.g. 'Calvin Japan stay plans', 'API outage November'). "
        "For exact identifiers (email, UUID, key, file path), pass them "
        "verbatim. Avoid full sentences — the search is keyword/semantic, "
        "not Q&A."
    ),
    scope=StringSchema(
        "Where to search. 'all' (default) covers both undreamed sources and "
        "dreamed memory entries.",
        enum=["all", "dreamed", "undreamed"],
    ),
    level=StringSchema(
        "How much content to return per result. 'warm' (default) returns "
        "headlines + summaries; 'cold' returns full bodies.",
        enum=["warm", "cold"],
    ),
    keywords=StringSchema(
        "Optional literal string that MUST appear in results "
        "(e.g. an email, UUID, exact phrase). When supplied, lexical "
        "matches against this string are weighted heavily so the exact "
        "hit surfaces robustly. Leave empty for purely semantic queries."
    ),
    limit=IntegerSchema(
        10,
        description=(
            "Max results to return. Default 10. Increase to 20-30 for "
            "audit / investigative queries; reduce for chat-style short "
            "answers. Hard cap 50 — higher values consume many tokens."
        ),
        minimum=1,
        maximum=50,
    ),
    required=["query"],
    description=(
        # Canonical text per `docs/memory/06_prompts_and_instructions.md` §3.1.
        # Synchronisation enforced by `tests/memory/test_tool_description_sync.py`.
        "Search durin's memory for content relevant to your question. "
        "Searches across canonical entity pages, recent observations, "
        "session summaries, and ingested documents in one call.\n\n"
        "Usage:\n"
        "- For most queries, use a single call with a natural-language `query`.\n"
        "- For multi-part questions, issue 2-3 calls with different phrasings "
        "rather than one long query.\n"
        "- For literal-match queries (emails, IDs, URLs), pass the literal "
        "string in `keywords` in addition to a natural-language `query`. "
        "This biases the search toward exact matches.\n"
        "- Use `level: \"cold\"` only when you need full body content "
        "(verbose; consumes many tokens). `warm` (default) returns "
        "headline + summary, enough for most tasks.\n"
        "- `limit` defaults to 10. Reduce to 3-5 for chat-style short "
        "answers, raise to 20-30 for audit / investigative queries that "
        "need to see every relevant hit. Hard cap 50.\n\n"
        "Results come pre-sectioned with structural markers:\n"
        "- `=== CANONICAL: <uri> ===` — consolidated entity pages "
        "(durable knowledge)\n"
        "- `=== FRAGMENT: <path> ===` — recent observations not yet "
        "consolidated\n"
        "- `=== SESSION: <id> ===` — conversation summaries\n"
        "- `=== INGESTED: <id> ===` — chunks of documents the user has "
        "loaded\n\n"
        "When sources disagree, more recent fragments may reflect updates "
        "that have not yet been consolidated into the canonical entity "
        "page. Use timestamps in the markers to reason about recency.\n\n"
        "State the source of any fact you cite (uri or section marker) "
        "in parentheses. Do not claim facts that are not in the search "
        "results."
    ),
)


@tool_parameters(_PARAMETERS)
class MemorySearchTool(Tool):
    """memory_search tool — locate memories and source turns by substring."""

    config_key = "memory"

    @property
    def read_only(self) -> bool:
        return True

    def __init__(
        self,
        workspace: str | Path,
        embedding_model: str | None = None,
        *,
        app_config: Any | None = None,
        aux_provider_handle: Any | None = None,
    ) -> None:
        self._workspace = Path(workspace).expanduser()
        self._embedding_model = embedding_model
        self._vector_index: Optional[VectorIndex] = None
        self._vector_index_attempted = False
        # G3.b: stash refs needed to resolve the memory LLM model at
        # rewrite time. ``app_config`` carries the full DurinConfig so
        # we can pick aux_models.memory or fall back to the agent
        # preset. ``aux_provider_handle`` is the pre-built handle from
        # AgentLoop._build_aux_providers (avoids rebuilding the
        # provider per search call). Both default to None so test
        # paths that construct the tool directly keep working with
        # the legacy default_llm_invoke fallback.
        self._app_config = app_config
        self._aux_provider_handle = aux_provider_handle
        # Per doc 25 §2.C: alias index is shared process-wide via
        # durin.memory.aliases_cache, so DreamConsolidator and
        # EntityAbsorption see updates as soon as we (or they) call
        # refresh_for / remove on it. No per-instance state needed.
        # Schema-version freshness check (doc 10 P2.2): on first
        # construction per process per workspace, ensure the FTS
        # index matches the code's CURRENT_SCHEMA_VERSION; auto-
        # rebuild if not. The helper is idempotent.
        try:
            from durin.memory.indexer import ensure_index_fresh
            ensure_index_fresh(self._workspace)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "memory_search: ensure_index_fresh failed: %s", exc,
            )

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search the agent's memory. Pass a short topical phrase (2-6 "
            "words) or an exact identifier — semantic retrieval when the "
            "vector index is built, substring fallback otherwise. For "
            "multi-part or compound questions, issue 2-3 searches with "
            "different phrasings rather than one long query. "
            "scope='dreamed' covers memory/<class>/*.md (consolidated "
            "learnings); scope='undreamed' covers sessions/<key>.md and "
            "ingested/<id>/; scope='all' is both. level='warm' returns "
            "headlines+summaries; level='cold' adds full bodies (use when "
            "warm hits look on-topic but you need the detail). Returns "
            "markdown URIs usable with memory_drill."
        )

    def _build_cross_encoder(self):
        """Construct a :class:`CrossEncoderReranker` when enabled in
        config; otherwise return None.

        Lazy + cached per instance: the model load happens on first
        :meth:`execute` that opts in. Re-building per call is cheap
        (just a wrapper object) but the underlying model loads only
        once via the scorer's lazy-load.
        """
        if self._app_config is None:
            return None
        try:
            ce_cfg = (
                self._app_config.memory.search.cross_encoder
            )
        except AttributeError:
            return None
        if not getattr(ce_cfg, "enabled", False):
            return None
        if getattr(self, "_cross_encoder_cache", None) is not None:
            return self._cross_encoder_cache
        from durin.memory.cross_encoder import CrossEncoderReranker
        self._cross_encoder_cache = CrossEncoderReranker(
            model=ce_cfg.model,
            batch_size=int(ce_cfg.batch_size or 32),
        )
        return self._cross_encoder_cache

    def _enrich_body(self, r: Result) -> Result:
        """Populate ``body`` on a vector-shaped Result by loading the entry.

        Vector index stores ``summary``/``headline`` but not the full
        body — for cold-tier callers we read the markdown back. The
        ``uri`` shape is ``memory/<class>/<entry_id>``; we map that to
        ``<workspace>/memory/<class>/<entry_id>.md``. Returns the
        original result unchanged when the file is missing or unreadable
        (don't break the result set over a single bad entry).
        """
        import dataclasses

        from durin.memory.storage import load_entry

        try:
            _, class_name, entry_id = r.uri.split("/", 2)
        except ValueError:
            return r
        path = self._workspace / "memory" / class_name / f"{entry_id}.md"
        if not path.is_file():
            return r
        try:
            entry = load_entry(path)
        except Exception:  # noqa: BLE001
            return r
        return dataclasses.replace(r, body=entry.body)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        # Vector retrieval is opt-in (memory.enabled); see memory_store.
        # ``ctx.config`` carries ``tools`` only — the memory section lives
        # on ``ctx.app_config`` (full DurinConfig). Tests that bypass
        # AgentLoop and build a bare ToolContext leave ``app_config=None``,
        # which intentionally disables the vector path (grep fallback).
        model = None
        app = getattr(ctx, "app_config", None)
        try:
            if app is not None and app.memory.enabled:
                model = app.memory.embedding.model
        except (AttributeError, TypeError):
            model = None
        # G3.b: forward the memory aux provider (if AgentLoop pre-built
        # one) so the rewriter can avoid spinning up a fresh provider
        # per search call.
        aux_handle = None
        aux_providers = getattr(ctx, "aux_providers", None)
        if isinstance(aux_providers, dict):
            aux_handle = aux_providers.get("memory")
        return cls(
            workspace=ctx.workspace,
            embedding_model=model,
            app_config=app,
            aux_provider_handle=aux_handle,
        )

    def _get_vector_index(self) -> Optional[VectorIndex]:
        if self._vector_index_attempted:
            return self._vector_index
        self._vector_index_attempted = True
        if not self._embedding_model or not vector_index_available():
            return None
        try:
            from durin.memory.embedding import FastembedProvider

            provider = FastembedProvider(model=self._embedding_model)
            self._vector_index = VectorIndex(self._workspace, provider)
        except Exception as exc:
            logger.warning("vector index init failed: %s", exc)
            self._vector_index = None
        return self._vector_index

    def _get_alias_index(self) -> Optional[AliasIndex]:
        """Resolve the workspace-shared AliasIndex (doc 25 §2.C).

        Built lazily on first call across the whole process via
        :func:`durin.memory.aliases_cache.get_shared_alias_index`;
        ``DreamConsolidator`` and ``EntityAbsorption`` consult the same
        instance so a single workspace builds the index once even when
        multiple consumers hit it in the same ``durin agent`` run.

        Returns ``None`` when the index is empty — entity-aware
        reranking is a no-op against an empty alias map, so surfacing
        ``None`` lets the upstream code skip the rerank step entirely.
        """
        try:
            from durin.memory.aliases_cache import get_shared_alias_index

            idx = get_shared_alias_index(self._workspace / "memory")
        except Exception as exc:  # noqa: BLE001
            logger.warning("alias_index resolve failed: %s", exc)
            return None
        return idx if idx.size() > 0 else None

    async def execute(self, **kwargs: Any) -> Any:
        query = str(kwargs.get("query") or "").strip()
        scope = str(kwargs.get("scope") or "all")
        level = str(kwargs.get("level") or "warm")
        keywords_raw = kwargs.get("keywords")
        keywords = (
            str(keywords_raw).strip()
            if isinstance(keywords_raw, str) and keywords_raw.strip()
            else None
        )

        # `limit` is clamped to [1, 50] defensively even though the
        # schema declares minimum/maximum — the LLM occasionally emits
        # values outside the declared bounds, and a 50-row cap protects
        # the response from token blow-up regardless.
        try:
            limit_raw = kwargs.get("limit")
            limit = 10 if limit_raw is None else int(limit_raw)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(50, limit))

        if not query:
            return {"error": "query is required"}
        if scope not in ("all", "dreamed", "undreamed"):
            return {"error": f"invalid scope {scope!r}"}
        if level not in ("warm", "cold"):
            return {"error": f"invalid level {level!r}"}

        # v2 pipeline (Phase 5 d1): delegate the whole search to
        # `run_search_pipeline` — query router + lexical FTS + vector +
        # cross-source RRF + entity-aware rerank + grep fallback +
        # sectioning + per-source cap. Doc 03 (full pipeline).
        from durin.memory.search_pipeline import run_search_pipeline

        vi = self._get_vector_index() if scope in ("dreamed", "all") else None

        # Cross-encoder rerank is opt-in via config (doc 03 §9). When
        # enabled, build a reranker lazily and pass it through. The
        # pipeline gracefully no-ops if the model fails to load.
        cross_encoder = self._build_cross_encoder()
        ce_top_n = 10
        if (
            self._app_config is not None
            and getattr(self._app_config, "memory", None) is not None
        ):
            ce_cfg = getattr(
                self._app_config.memory.search, "cross_encoder", None,
            )
            if ce_cfg is not None:
                ce_top_n = int(getattr(ce_cfg, "top_n", 10) or 10)

        t0 = time.monotonic()
        pipeline_result = run_search_pipeline(
            self._workspace,
            query,
            keywords=keywords,
            vector_index=vi,
            limit=limit,
            cross_encoder=cross_encoder,
            cross_encoder_top_n=ce_top_n,
        )
        duration_ms = (time.monotonic() - t0) * 1000.0

        # Preserve `memory.recall.vector` telemetry (consumed by
        # `durin memory stats`'s vector_total counter). Emitted
        # whenever the vector path was attempted — matches the v1
        # behaviour where the event fired regardless of hit count.
        if vi is not None:
            _ai = self._get_alias_index()
            qents = (
                extract_query_entities(query, _ai)
                if _ai is not None else []
            )
            ranking_label = "entity_aware" if qents else "default"
            emit_tool_event(
                "memory.recall.vector",
                {
                    "query": query,
                    "scope": scope,
                    "embedding_model": self._embedding_model or "",
                    "hit_count": pipeline_result.vector_count,
                    "duration_ms": duration_ms,
                    "ranking": ranking_label,
                    "query_entities_count": len(qents),
                    "reordered": False,
                    "top_1_id_before": "",
                    "top_1_id_after": "",
                },
            )

        # `scope=undreamed` mode is a v1 niche — the orchestrator's
        # grep step covers sessions + ingested but mixes them with
        # dreamed memory hits. When the caller wants ONLY undreamed,
        # filter the result set down.
        hits = pipeline_result.hits
        if scope == "undreamed":
            hits = [
                h for h in hits
                if h.type in ("session_summary", "corpus")
            ]

        # Convert :class:`SectionedHit` rows into the legacy `Result`
        # shape expected by the agent (carries `to_dict` + `render_block`).
        results: list[Result] = []
        for h in hits:
            r = self._sectioned_to_result(h, level=level)
            if r is not None:
                results.append(r)

        # Strategy / ranking labels for downstream telemetry consumers
        # that pattern-match. We derive them from what the pipeline
        # actually used so the labels reflect reality, not heuristics.
        if pipeline_result.vector_count and pipeline_result.lexical_count:
            strategy = "hybrid"
        elif pipeline_result.vector_count:
            strategy = "vector"
        elif pipeline_result.lexical_count:
            strategy = "lexical"
        else:
            strategy = "grep"
        ranking = "default"
        ai = self._get_alias_index()
        if ai is not None and extract_query_entities(query, ai):
            ranking = "entity_aware"

        emit_tool_event(
            "memory.recall",
            {
                "query": query,
                "scope": scope,
                "level": level,
                "result_count": len(results),
            },
        )
        response: dict[str, Any] = {
            "results": [
                {**r.to_dict(), "rendered": r.render_block()}
                for r in results
            ],
            "total": len(results),
            "strategy": strategy,
            "ranking": ranking,
        }
        # P5.2: surface degraded-run info when the pipeline recovered
        # from a source failure. Omitted on clean runs to keep the
        # response shape minimal.
        if pipeline_result.recovered_from:
            response["recovered_from"] = list(pipeline_result.recovered_from)
            response["recovery_duration_ms"] = (
                pipeline_result.recovery_duration_ms
            )
        return response

    def _sectioned_to_result(
        self, hit: Any, *, level: str,
    ) -> Optional[Result]:
        """Convert a :class:`durin.memory.sectioned_output.SectionedHit`
        into a legacy :class:`Result` for the tool's response shape.

        - Loads the body from disk on `level=cold` (the search pipeline
          carries snippet only).
        - Maps `entity` → `class_name='entity_page'` to preserve the
          §2.H rendering contract (canonical vs fragment markers).
        """
        # Derive the legacy class_name + uri + source shape.
        hit_path = hit.path or ""
        if hit.type == "entity":
            class_name = "entity_page"
            # `hit.uri` for entity pages is `<type>:<slug>`; the legacy
            # URI shape carries the class prefix.
            uri = (
                hit.uri if hit.uri.startswith("memory/entity_page/")
                else f"memory/entity_page/{hit.uri}"
            )
            source = "memory"
        elif hit.type in ("session_summary", "session") or (
            hit_path.startswith("sessions/") or "sessions/" in hit_path
        ):
            class_name = hit.type or "session_summary"
            uri = hit_path or hit.uri
            source = "sessions"
        elif "ingested/" in hit_path:
            class_name = hit.type or "corpus"
            uri = hit_path or f"memory/corpus/{hit.uri}"
            source = "ingested"
        else:
            class_name = hit.type or ""
            # FTS hits already carry the `memory/<class>/<id>` prefix
            # (set by the indexer, P2.2 follow-up); grep hits do too
            # (via search.search_memory). Don't double-prefix.
            if hit.uri.startswith(f"memory/{class_name}/") or hit.uri.startswith(
                "memory/"
            ):
                uri = hit.uri
            else:
                uri = (
                    f"memory/{class_name}/{hit.uri}"
                    if class_name else f"memory/{hit.uri}"
                )
            source = "memory"

        entities = (hit.uri,) if class_name == "entity_page" else ()
        # P2.5: prefer the body the search pipeline already carries
        # (populated from the LanceDB row). Falls back to disk read
        # via `_enrich_body` only when the vector index didn't have
        # the row (e.g. grep-only path).
        carried_body = getattr(hit, "body", "") or ""
        result = Result(
            source=source,
            uri=uri,
            headline=hit.snippet or hit.uri,
            snippet=(hit.snippet or "")[:160],
            summary=hit.snippet or "",
            body=carried_body if level == "cold" else "",
            class_name=class_name,
            valid_from=hit.ts or "",
            entities=entities,
        )
        if level == "cold" and not result.body:
            result = self._enrich_body(result)
        return result


def _vector_row_to_result(row: dict) -> Result:
    """Shape a LanceDB row to match the grep Result schema.

    Per doc 25 §2.H: preserve ``class_name`` / ``valid_from`` /
    ``entities`` so the LLM-facing :meth:`Result.to_dict` and
    :meth:`Result.render_block` can mark the row as canonical vs
    fragment. Earlier versions dropped these fields, breaking the
    contract that doc 18 §6 ("LLM reconcilia con timestamps y
    contexto") implied.
    """
    class_name = row.get("class_name", "")
    entry_id = row.get("id", "")
    summary = row.get("summary", "") or ""
    headline = row.get("headline", "") or ""
    valid_from = row.get("valid_from", "") or ""
    raw_entities = row.get("entities") or []
    entities = tuple(str(e) for e in raw_entities)
    return Result(
        source="memory",
        uri=f"memory/{class_name}/{entry_id}",
        headline=headline,
        snippet=(summary[:160] if summary else headline)[:160],
        summary=summary,
        body="",
        class_name=class_name,
        valid_from=valid_from,
        entities=entities,
    )
