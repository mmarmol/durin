"""memory_search tool — Phase-1 grep over memory entries and session views."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_ranker import (
    extract_query_entities,
)
from durin.memory.index_meta import skills_indexing_enabled
from durin.memory.search import Result
from durin.memory.vector_index import VectorIndex, vector_index_available

logger = logging.getLogger(__name__)


def _skill_uri_to_path(uri: str) -> str:
    """Normalise a skill uri to its drillable on-disk shape.

    Skill hits surface under two uri forms depending on the source:
    the FTS/vector internal id ``skill/<slug>`` and the on-disk path
    ``skills/<slug>/SKILL.md`` (what the grep fallback emits). Both map
    to the same file; return the ``skills/<slug>/SKILL.md`` form so the
    agent always receives a drillable uri.
    """
    if uri.startswith("skills/") and uri.endswith("/SKILL.md"):
        return uri
    if uri.startswith("skill/"):
        from durin.memory.paths import skill_path_from_uri

        return skill_path_from_uri(uri)
    return uri


# E11 (2026-05-28): `_load_cursors_from_entities_dir` moved to
# `durin.memory.entity_ranker` next to its consumer
# `rank_with_entities`. The v2 pipeline now calls it directly.

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
        "dreamed memory entries. 'archive' walks `memory/archive/` on demand "
        "for recovery / diagnostic queries against consolidated content "
        "(audit F1, doc 01 §3.6).",
        enum=["all", "dreamed", "undreamed", "archive"],
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
    kinds=StringSchema(
        "Which kinds of results to return. 'all' (default) returns "
        "everything. 'skill' returns only skill procedures. 'fact' "
        "returns everything EXCEPT skills (facts, entity pages, "
        "sessions, ingested docs).",
        enum=["all", "skill", "fact"],
    ),
    required=["query"],
    description=(
        # Canonical text per `docs/architecture/memory/06_prompts_and_instructions.md` §3.1.
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
        "- For exact phrase matching, wrap the phrase in double quotes "
        "inside `query` — e.g. `\"shooting percentage\" basketball` "
        "requires the two words to appear adjacent and in order, while "
        "`basketball` matches anywhere. Words outside quotes stay as "
        "loose tokens. An unbalanced quote is treated as a typo and "
        "discarded.\n"
        "- Use `level: \"cold\"` only when you need full body content "
        "(verbose; consumes many tokens). `warm` (default) returns "
        "headline + summary, enough for most tasks.\n"
        "- `limit` defaults to 10. Reduce to 3-5 for chat-style short "
        "answers, raise to 20-30 for audit / investigative queries that "
        "need to see every relevant hit. Hard cap 50.\n\n"
        "Results come pre-sectioned with structural markers:\n"
        "- `=== SKILL: <name> ===` — a matching procedure; these are "
        "steps to FOLLOW, not facts to cite\n"
        "- `=== CANONICAL: <uri> ===` — consolidated entity pages "
        "(durable knowledge)\n"
        "- `=== FRAGMENT: <path> ===` — recent observations not yet "
        "consolidated\n"
        "- `=== SESSION: <id> ===` — conversation summaries\n"
        "- `=== INGESTED: <id> ===` — chunks of documents the user has "
        "loaded\n\n"
        "Each marker also carries a completeness qualifier:\n"
        "- `(complete)` — the body shown IS the full entry; do NOT call "
        "memory_drill on this uri, it returns the same text.\n"
        "- `(preview N/M)` — N chars shown, M chars exist; call "
        "memory_drill on this uri only if you need the remaining body.\n"
        "Markers without a completeness qualifier are rare (legacy / "
        "lexical-only hits) — use judgment.\n\n"
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
        context_dedup: bool = False,
    ) -> None:
        self._workspace = Path(workspace).expanduser()
        self._embedding_model = embedding_model
        self._vector_index: Optional[VectorIndex] = None
        self._vector_index_attempted = False
        self._app_config = app_config
        # P4 (2026-06-10): hot-layer dedup only makes sense when the
        # CALLER's system prompt actually carries the hot layer, so it
        # is opt-in: `create()` (the agent tool path) enables it for
        # the core scope. Direct constructions — graph_api / webui
        # search, subagents, ad-hoc scripts — face callers with no hot
        # layer in front of them and must see every hit.
        self._context_dedup = context_dedup
        # Per doc 25 §2.C: alias index is shared process-wide via
        # durin.memory.aliases_cache, so the refine pass and
        # EntityAbsorption see updates as soon as we (or they) call
        # refresh_for / remove on it. No per-instance state needed.
        # Schema-version freshness check (doc 10 P2.2): on first
        # construction per process per workspace, ensure the FTS
        # index matches the code's CURRENT_SCHEMA_VERSION; auto-
        # rebuild if not. The helper is idempotent.
        try:
            from durin.memory.indexer import ensure_index_fresh
            ensure_index_fresh(self._workspace, embedding_model=self._embedding_model)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "memory_search: ensure_index_fresh failed: %s", exc,
            )

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        # Canonical text per `docs/architecture/memory/06_prompts_and_instructions.md` §3.1.
        # `Tool.to_schema()` (durin/agent/tools/base.py:258) reads this and
        # emits it as `function.description` in the OpenAI function-calling
        # spec — that's the description the LLM actually reads to decide
        # whether to call the tool. Synchronisation enforced by
        # `tests/memory/test_tool_description_sync.py`. Audit B1 (2026-05-28)
        # caught that the prior short text here diverged from the canonical
        # doc; the long form below now matches doc 06 §3.1 verbatim.
        return _PARAMETERS["description"]

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

        # Skill hits address `skills/<name>/SKILL.md`, not the
        # `memory/<class>/<id>` triplet the split below expects. Read the
        # SKILL.md directly so cold-tier callers get a real body instead
        # of an empty string (the split would mis-parse the skill uri).
        if r.class_name == "skill":
            skill_path = self._workspace / _skill_uri_to_path(r.uri)
            if not skill_path.is_file():
                return r
            try:
                text = skill_path.read_text(encoding="utf-8")
            except OSError:
                return r
            return dataclasses.replace(r, body=text)

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
        return cls(
            workspace=ctx.workspace,
            embedding_model=model,
            app_config=app,
            context_dedup=getattr(ctx, "scope", "core") != "subagent",
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
        the refine pass and ``EntityAbsorption`` consult the same
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
        kinds = str(kwargs.get("kinds") or "all")
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
        if scope not in ("all", "dreamed", "undreamed", "archive"):
            return {"error": f"invalid scope {scope!r}"}
        if level not in ("warm", "cold"):
            return {"error": f"invalid level {level!r}"}
        if kinds not in ("all", "skill", "fact"):
            return {"error": f"invalid kinds {kinds!r}"}

        # F2 (audit third pass, 2026-05-28): archive is intentionally
        # not indexed (vector/lexical/grep over memory/ exclude
        # `memory/archive/**`). The `scope='archive'` surface is a
        # separate on-demand walk for recovery / diagnostic queries.
        # No re-ranking, no entity-aware — substring match over
        # headline + summary + body of each archived `.md`.
        if scope == "archive":
            return self._run_archive_scope(query, limit=limit)

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

        # G1 (audit fourth pass, 2026-05-28): operator-configured
        # per-source cap for the sectioning step. Default is None →
        # `run_search_pipeline` falls back to
        # `DEFAULT_MAX_PER_SOURCE` so existing workspaces are
        # unchanged.
        max_per_source: int | None = None
        if self._app_config is not None:
            try:
                sectioning_cfg = (
                    self._app_config.memory.search.sectioning
                )
                max_per_source = int(sectioning_cfg.max_per_source)
            except AttributeError:
                max_per_source = None

        t0 = time.monotonic()
        pipeline_result = run_search_pipeline(
            self._workspace,
            query,
            keywords=keywords,
            vector_index=vi,
            limit=limit,
            cross_encoder=cross_encoder,
            cross_encoder_top_n=ce_top_n,
            max_per_source=max_per_source,
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

        # M1 (2026-06-03): read-side gate for `memory.index_skills=False`.
        # The write-side gates stop NEW skills from being indexed, but a
        # skill indexed earlier (while the flag was True) leaves FTS/vector
        # rows that the search arms still read — and drift repair does not
        # evict them. Drop ALL skill-typed hits at the tool boundary (the
        # only skill-surfacing consumer) so flipping the flag off yields
        # zero skill hits immediately, regardless of lingering rows. This
        # sits BEFORE both the `results` conversion and `render_sectioned`,
        # so neither the payload nor the rendered text leaks a skill.
        if not skills_indexing_enabled():
            hits = [h for h in hits if h.type != "skill"]

        # `kinds` post-filter: 'skill' keeps only skill procedures,
        # 'fact' drops them (facts/entities/sessions/ingested), 'all'
        # (default) is a no-op. Skill hits carry `type == "skill"`.
        if kinds == "skill":
            hits = [h for h in hits if h.type == "skill"]
        elif kinds == "fact":
            hits = [h for h in hits if h.type != "skill"]

        # Convert :class:`SectionedHit` rows into the legacy `Result`
        # shape expected by the agent (carries `to_dict`). F4 (third
        # pass, 2026-05-28): the LLM-facing block rendering moved to
        # `sectioned_output.render_sectioned` so the per-source cap
        # (doc 03 §12.4) and section intros (§12) actually activate.
        results: list[Result] = []
        for h in hits:
            r = self._sectioned_to_result(h, level=level)
            if r is not None:
                results.append(r)

        # F4: apply per-source cap + render sectioned output.
        from durin.memory.sectioned_output import (
            SectionedHit,
            apply_per_source_cap,
            render_sectioned,
        )
        _type_from_class = {
            "skill": "skill",
            "entity_page": "entity",
            "episodic": "episodic", "stable": "stable",
            "corpus": "corpus", "session_summary": "session_summary",
        }
        enriched_hits = [
            SectionedHit(
                uri=r.uri,
                type=_type_from_class.get(r.class_name, "episodic"),
                path=r.uri,
                score=0.0,
                ts=r.valid_from,
                snippet=r.snippet,
                body=r.body,
                summary=r.summary,
                entities=tuple(r.entities),
                ingest_id=None,
            )
            for r in results
        ]
        capped_hits = apply_per_source_cap(enriched_hits)

        # P4 (2026-06-10): hits whose rendered content is already
        # visible in the caller's hot layer collapse to pointer lines.
        # Containment-checked per ref — a hit carrying body beyond the
        # prefix excerpt passes through whole. Disabled for subagents
        # (their prompt has no hot layer; see __init__).
        in_context_hits: list[SectionedHit] = []
        if self._context_dedup:
            from durin.memory.context_dedup import split_in_context
            capped_hits, in_context_hits = split_in_context(
                self._workspace, capped_hits,
            )

        kept_uris = {h.uri for h in capped_hits}
        results = [r for r in results if r.uri in kept_uris]
        sectioned_rendered = render_sectioned(capped_hits)
        if in_context_hits:
            from durin.memory.context_dedup import (
                render_in_context_section,
            )
            pointer_section = render_in_context_section(in_context_hits)
            sectioned_rendered = (
                f"{sectioned_rendered}\n\n{pointer_section}"
                if sectioned_rendered else pointer_section
            )

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

        # E1: expand payload to match doc 07 §4.1 — all diagnostic
        # fields are already computed above; this is a payload
        # change, not new instrumentation.
        total_candidates = (
            pipeline_result.vector_count + pipeline_result.lexical_count
        )
        recall_payload: dict[str, Any] = {
            "query": query,
            "scope": scope,
            "level": level,
            "result_count": len(results),
            "strategy": strategy,
            "duration_ms": duration_ms,
            "total_candidates": total_candidates,
            "skill_result_count": sum(1 for r in results if r.kind == "skill"),
            "keywords": keywords,
            "in_context_deduped": len(in_context_hits),
        }
        if pipeline_result.recovered_from:
            recall_payload["recovered_from"] = list(
                pipeline_result.recovered_from,
            )
            recall_payload["recovery_duration_ms"] = (
                pipeline_result.recovery_duration_ms
            )
        emit_tool_event("memory.recall", recall_payload)
        if kinds == "skill" and not results:
            from durin.memory.paths import walk_skills
            had_candidate = any(True for _ in walk_skills(self._workspace))
            emit_tool_event(
                "memory.skill_miss",
                {
                    "query": query,
                    "result_count": 0,
                    "had_skill_candidate": had_candidate,
                },
            )
        response: dict[str, Any] = {
            "results": [r.to_dict() for r in results],
            "total": len(results),
            "strategy": strategy,
            "ranking": ranking,
            "sectioned_rendered": sectioned_rendered,
        }
        if in_context_hits:
            response["already_in_context"] = [
                h.uri for h in in_context_hits
            ]
        # P5.2: surface degraded-run info when the pipeline recovered
        # from a source failure. Omitted on clean runs to keep the
        # response shape minimal.
        if pipeline_result.recovered_from:
            response["recovered_from"] = list(pipeline_result.recovered_from)
            response["recovery_duration_ms"] = (
                pipeline_result.recovery_duration_ms
            )
        return response

    def _run_archive_scope(
        self, query: str, *, limit: int,
    ) -> dict[str, Any]:
        """F2 (audit third pass, 2026-05-28): on-demand walk of
        `memory/archive/**` for `scope='archive'` queries.

        Archive is intentionally not indexed (doc 01 §3.6). This path
        loads each archived `.md`, substring-matches the query against
        headline + summary + body, and returns up to `limit` hits.

        No vector / lexical / cross-encoder — recovery surface,
        not the hot path. The shape mirrors the normal response so the
        agent renders it the same way (`results`, `total`, `strategy`).
        """
        import re

        from durin.memory.storage import (
            FrontmatterError,
            split_frontmatter,
        )

        archive_root = self._workspace / "memory" / "archive"
        if not archive_root.is_dir():
            return {
                "results": [], "total": 0,
                "strategy": "archive", "ranking": "default",
            }

        needle = query.lower()
        hits: list[Result] = []
        for path in sorted(archive_root.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            # Parse the frontmatter shallowly. Archive .md files always
            # carry a YAML block; we only need headline / summary /
            # name / aliases for matching + rendering.
            try:
                front, body = split_frontmatter(text)
            except FrontmatterError:
                continue
            haystack = " ".join((
                front.get("headline", ""),
                front.get("summary", ""),
                front.get("name", ""),
                " ".join(front.get("aliases", []) or []),
                body,
            )).lower()
            if needle not in haystack:
                continue
            # Determine class_name from the archive subpath:
            # archive/episodic/...  → episodic
            # archive/entities/...  → entity_page
            # G6 (audit fourth pass, 2026-05-28): emit the relative
            # path under `memory/` as the URI so the agent can drill
            # archive hits directly. Pre-G6 the URI was
            # `front.uri or path.stem` — a bare id with no path
            # prefix that drill could not resolve, so the agent had
            # no way to fetch the full body of an archive hit.
            try:
                rel_path = path.relative_to(self._workspace).as_posix()
            except ValueError:
                rel_path = f"memory/archive/{path.name}"
            try:
                rel_parts = path.relative_to(archive_root).parts
            except ValueError:
                rel_parts = ()
            if rel_parts and rel_parts[0] == "entities":
                class_name = "entity_page"
                headline = front.get("name", path.stem)
            else:
                class_name = (
                    rel_parts[0] if rel_parts else "archived"
                )
                headline = front.get("headline", path.stem)
            uri = rel_path
            summary = front.get("summary", "")
            # Snippet: first ~160 chars around the match.
            m = re.search(re.escape(needle), haystack)
            if m:
                start = max(0, m.start() - 80)
                end = min(len(haystack), m.end() + 80)
                snippet = haystack[start:end]
            else:
                snippet = (body or summary)[:160]
            hits.append(Result(
                source="memory",
                uri=uri,
                headline=headline,
                snippet=snippet,
                summary=summary,
                body=body,
                class_name=class_name,
                valid_from=str(front.get("valid_from", "") or ""),
                entities=(),
            ))
            if len(hits) >= limit:
                break

        emit_tool_event(
            "memory.recall",
            {
                "query": query,
                "scope": "archive",
                "level": "warm",
                "result_count": len(hits),
                "strategy": "archive",
                "duration_ms": 0.0,
                "total_candidates": len(hits),
                "keywords": None,
            },
        )
        # F4: archive path also uses sectioned rendering for parity
        # with the main path. Map each Result to a SectionedHit and
        # call render_sectioned. Per-source cap rarely triggers on
        # archive but applying it keeps the path uniform.
        from durin.memory.sectioned_output import (
            SectionedHit,
            apply_per_source_cap,
            render_sectioned,
        )
        _archive_type_from_class = {
            "entity_page": "entity",
            "episodic": "episodic", "stable": "stable",
            "corpus": "corpus", "session_summary": "session_summary",
        }
        sectioned = [
            SectionedHit(
                uri=r.uri,
                type=_archive_type_from_class.get(
                    r.class_name, "episodic",
                ),
                path=r.uri,
                score=0.0,
                ts=r.valid_from,
                snippet=r.snippet,
                body=r.body,
                summary=r.summary,
                entities=tuple(r.entities),
                ingest_id=None,
            )
            for r in hits
        ]
        # G1 (audit fourth pass, 2026-05-28): honour the operator-
        # configured cap on the archive path too. In practice every
        # archived hit has `ingest_id=None` so the cap keys off `uri`
        # and rarely triggers, but threading the config keeps the two
        # paths uniform.
        archive_cap: int | None = None
        if self._app_config is not None:
            try:
                archive_cap = int(
                    self._app_config.memory.search.sectioning
                    .max_per_source
                )
            except AttributeError:
                archive_cap = None
        if archive_cap is None:
            capped = apply_per_source_cap(sectioned)
        else:
            capped = apply_per_source_cap(
                sectioned, max_per_source=archive_cap,
            )
        kept = {h.uri for h in capped}
        kept_results = [r for r in hits if r.uri in kept]
        return {
            "results": [r.to_dict() for r in kept_results],
            "total": len(kept_results),
            "strategy": "archive",
            "ranking": "default",
            "sectioned_rendered": render_sectioned(capped),
        }

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
        if hit.type == "skill":
            # Skills live under `skills/<name>/SKILL.md` (outside
            # `memory/`). The agent uri must be the drillable display
            # path regardless of which source surfaced the hit. B1
            # (2026-06-03) unified the FUSION uri to `skill/<slug>`, so
            # BOTH `hit.uri` and `hit.path` can now be `skill/<slug>`
            # (the grep arm sets `path = r.uri = skill/<slug>`); always
            # run `_skill_uri_to_path` over the chosen source so the
            # result uri is `skills/<slug>/SKILL.md`. Do NOT fall through
            # to the `memory/<class>/<id>` shaping below — that would
            # emit an undrillable `memory/skill/...` uri.
            class_name = "skill"
            uri = _skill_uri_to_path(hit_path or hit.uri)
            source = "memory"
        elif hit.type == "entity":
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
            class_name = hit.type or "ingested"
            uri = hit_path or hit.uri
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
