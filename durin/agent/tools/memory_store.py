"""memory_store tool — write a memory entry under memory/<class>/<id>.md."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    StringSchema,
    tool_parameters_schema,
)
from durin.memory.provenance import author_scope
from durin.memory.storage import load_entry
from durin.memory.store import StoreError, store_memory
from durin.memory.vector_index import VectorIndex, vector_index_available

logger = logging.getLogger(__name__)


def _session_turn_ref() -> str | None:
    """Provenance ref for the current foreground turn, or None.

    Reads the per-turn telemetry binding (the same one ``emit_tool_event``
    uses) for the active ``session_key`` + ``iteration`` and renders a
    wiki-link in the shape ``graph.py::_SESSION_REF_RE`` parses, so the
    stored entry links back to the session that created it. Returns None
    outside a bound turn (dream/compaction/internal writes), so those add
    no session ref. Never raises.
    """
    try:
        from durin.telemetry.logger import current_telemetry

        tl = current_telemetry()
        if tl is None or not tl.session_key:
            return None
        from durin.session.manager import SessionManager

        stem = SessionManager.safe_key(tl.session_key)
        return f"[[sessions/{stem}.md#turn-{tl.iteration}]]"
    except Exception:  # noqa: BLE001 — provenance is best-effort
        return None


# Agent-facing class enum. `pending` is a `MEMORY_CLASSES` value but is
# excluded here because the walker / indexer / file_watcher all skip
# `memory/pending/**` (intake buffer for compaction, not user-visible
# yet — see `paths.py::walk_memory`). Exposing `pending` to the LLM
# would let it write entries that the rest of the system never sees
# back — silent data loss. Internal callers that legitimately need to
# write under `pending` (e.g. compaction) use the pure `store_memory`
# function directly, bypassing the tool.
_AGENT_FACING_CLASSES = ("stable", "episodic", "corpus")

_PARAMETERS = tool_parameters_schema(
    content=StringSchema(
        "Markdown body of the memory entry — the full text to remember. "
        "Persisted as the `body` field of the resulting MemoryEntry."
    ),
    class_name=StringSchema(
        "Memory class — pick by lifespan and intent. Default: episodic. "
        "stable=identity, preferences, durable facts ('Marcelo lives in "
        "Spain'); episodic=working memory, recent events, conversation "
        "outcomes; corpus=chunks of inline reference text the user wants "
        "searchable (for files on disk use memory_ingest instead).",
        enum=list(_AGENT_FACING_CLASSES),
    ),
    headline=StringSchema(
        "Optional ~10-word headline. Auto-generated from the first ~10 "
        "words of `content` if omitted."
    ),
    summary=StringSchema(
        "Optional ~50-word summary returned by memory_search(level='warm')."
    ),
    source_refs=ArraySchema(
        StringSchema("markdown link"),
        description=(
            "Optional markdown links pointing back to the originating turn(s) or "
            "ingested doc section(s), e.g. "
            "[turn 42](../sessions/abc.md#turn-42)."
        ),
    ),
    entities=ArraySchema(
        StringSchema("entity reference"),
        description=(
            "Optional list, format '<type>:<slug>'. Examples: "
            "person:marcelo, project:durin, topic:embeddings, event:bug-X. "
            "Use lowercase slugs. Types are open vocabulary "
            "(person/place/project/topic/event/artifact/stance/practice "
            "are common but not exhaustive)."
        ),
    ),
    force=BooleanSchema(
        description=(
            "Set true to skip the near-duplicate similarity check. "
            "Default false. Use when you intentionally want to store "
            "near-identical content (e.g. reaffirming a fact)."
        ),
        default=False,
    ),
    required=["content"],
    description=(
        # Canonical text per `docs/architecture/memory/06_prompts_and_instructions.md` §3.2.
        "Persist an observation to memory. Use this when you learn a fact "
        "the user is likely to need again — preferences, decisions, facts "
        "about people/projects/ tasks, etc.\n\n"
        "Storage class (default: episodic):\n"
        "- `episodic`: working memory; short atomic observation. Most "
        "uses.\n"
        "- `stable`: durable, identity-level. Use sparingly — only when "
        "the user has explicitly said \"remember this\" or the fact is "
        "clearly identity-level.\n"
        "- `corpus`: chunks of inline reference text. For files on disk "
        "use memory_ingest instead — it preserves the original artifact "
        "and handles chunking.\n\n"
        "Always populate `entities` with the URIs this observation mentions "
        "(format: `<type>:<value>`, e.g., `person:marcelo`, `project:durin`). "
        "This enables entity-aware retrieval later.\n\n"
        "Keep `headline` short and specific — it can be omitted and the "
        "system will auto-generate one from the first ~10 words of "
        "`content`. `content` is the full body of the observation; don't "
        "truncate.\n\n"
        "If the user is restating something already known, do NOT call this "
        "tool — it creates duplicates. The Dream consolidation process will "
        "eventually fold duplicates but in the meantime they pollute "
        "results. A near-duplicate (cosine ≥ 0.95 of an existing entry) "
        "returns a warning instead of persisting; pass `force=true` only "
        "when you intentionally want to re-affirm an existing fact."
    ),
)


@tool_parameters(_PARAMETERS)
class MemoryStoreTool(Tool):
    """memory_store tool — persist distilled learnings as memory entries."""

    config_key = "memory"

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        # §8a: removed from the agent toolset in the new memory model. Facts
        # about a thing go through `memory_upsert_entity`; documents through
        # `memory_ingest`; interactions stay in the session for the dream to
        # distil. The `store_memory` FUNCTION stays for internal callers
        # (compaction summaries, ingest chunks).
        return False

    def __init__(
        self,
        workspace: str | Path,
        embedding_model: str | None = None,
        dream_config: Any | None = None,
        app_config: Any | None = None,
    ) -> None:
        self._workspace = Path(workspace).expanduser()
        self._embedding_model = embedding_model
        # Lazily constructed once on first use; None means "disabled".
        self._vector_index: Optional[VectorIndex] = None
        self._vector_index_attempted = False
        # Retained for constructor compatibility only. They fed the per-entity
        # threshold dream trigger, which was removed (§8e — the daily extract/
        # refine passes consolidate now); memory_store itself is disabled in the
        # new model.
        self._dream_config = dream_config
        self._app_config = app_config

    @property
    def name(self) -> str:
        return "memory_store"

    @property
    def description(self) -> str:
        # Canonical text per `docs/architecture/memory/06_prompts_and_instructions.md` §3.2.
        # Reads via `Tool.to_schema()` → `function.description` in the
        # OpenAI spec — what the LLM sees. Audit B1 (2026-05-28) caught
        # the prior short text drifted from the canonical doc.
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        # Vector retrieval is opt-in (memory.enabled). When off, pass no
        # embedding model so `_get_vector_index` stays None and the tool
        # degrades to markdown-only memory.
        # Read from ``ctx.app_config`` (full DurinConfig) — ``ctx.config``
        # only carries ``cfg.tools`` and has no memory section.
        model = None
        dream_cfg = None
        app = getattr(ctx, "app_config", None)
        if app is not None:
            try:
                if app.memory.enabled:
                    model = app.memory.embedding.model
            except (AttributeError, TypeError):
                model = None
            try:
                dream_cfg = app.memory.dream
            except (AttributeError, TypeError):
                dream_cfg = None
        return cls(
            workspace=ctx.workspace,
            embedding_model=model,
            dream_config=dream_cfg,
            app_config=app,
        )

    def _get_vector_index(self) -> Optional[VectorIndex]:
        """Lazy construct the VectorIndex once; returns None if disabled."""
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

    # Near-duplicate threshold. LanceDB L2 distance for unit-normalized
    # vectors satisfies L2² = 2(1 - cosine); fastembed paraphrase-
    # multilingual-MiniLM-L12-v2 emits unit vectors (validated by
    # scripts/test_embedding_name_variations.py). So:
    #   distance 0.10 ≈ cosine 0.95   (this threshold)
    #   distance 0.05 ≈ cosine 0.975
    #   distance 0.20 ≈ cosine 0.90
    # Matches OpenClaw's 0.95 cosine dedup convention (doc 22 N3 + G1).
    _DEDUP_DISTANCE_THRESHOLD = 0.10

    async def execute(self, **kwargs: Any) -> Any:
        content = str(kwargs.get("content", "")).strip()
        if not content:
            return {"error": "content is required"}

        class_name = str(kwargs.get("class_name") or "episodic")
        headline = kwargs.get("headline") or None
        summary = str(kwargs.get("summary") or "")
        source_refs = list(kwargs.get("source_refs") or [])
        # Provenance: when this runs inside a foreground turn, record the
        # originating session+turn so the entry links back to where it was
        # created (graph session→entity edge + the entry's backlinks).
        # Internal callers (dream/compaction) store via `store_memory`
        # directly with no bound telemetry context, so they add nothing.
        turn_ref = _session_turn_ref()
        if turn_ref and turn_ref not in source_refs:
            source_refs.append(turn_ref)
        entities = kwargs.get("entities") or []
        force = bool(kwargs.get("force", False))

        # Vector index dedup pre-check (per doc 23 T1.7 + OpenClaw N3).
        # Compute the embedding ONCE and reuse for both the dedup search
        # AND the post-write upsert (G5: avoid double-embedding cost).
        vi = self._get_vector_index()
        cached_vector: list[float] | None = None
        if vi is not None and not force:
            try:
                cached_vector = vi.embed_text(content)
                hits = vi.search_by_vector(cached_vector, top_k=1)
                if hits:
                    near = hits[0]
                    near_dist = float(near.get("_distance", 1.0))
                    if near_dist < self._DEDUP_DISTANCE_THRESHOLD:
                        # G6: block by default; the model can re-call with
                        # force=true if it genuinely wants the duplicate.
                        emit_tool_event(
                            "memory.store.blocked_near_duplicate",
                            {
                                "candidate_class_name": class_name,
                                "existing_id": str(near.get("id", "")),
                                "existing_class_name": str(near.get("class_name", "")),
                                "distance": near_dist,
                                "threshold": self._DEDUP_DISTANCE_THRESHOLD,
                            },
                        )
                        return {
                            "warning": "near-duplicate",
                            "nearest_id": near.get("id", ""),
                            "nearest_headline": near.get("headline", ""),
                            "nearest_distance": near_dist,
                            "hint": (
                                "Content is nearly identical to an existing "
                                "entry (cosine ≈ 0.95+). To store anyway, "
                                "re-call with force=true."
                            ),
                        }
            except Exception as exc:
                # Dedup check failure must NOT block the write — markdown
                # is the source of truth, vectors are derivable.
                logger.warning("memory_store dedup check failed: %s", exc)
                cached_vector = None  # fall through to recompute on upsert

        # The agent invoking this tool is the author — mark it explicitly so
        # the curator and dream can later distinguish agent-created entries
        # from user-edited ones.
        try:
            with author_scope("agent_created"):
                result = store_memory(
                    self._workspace,
                    content=content,
                    class_name=class_name,
                    headline=headline,
                    summary=summary,
                    source_refs=list(source_refs),
                    entities=list(entities),
                )
        except StoreError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"io error: {exc}"}

        emit_tool_event(
            "memory.store",
            {
                "entry_id": result["id"],
                "class_name": result["class"],
                "author": result["author"],
                "headline": result["headline"],
            },
        )

        # Best-effort vector upsert. A failure here must not break the
        # write path — the markdown file is the source of truth and the
        # index can always be rebuilt from it.
        if vi is not None:
            try:
                entry_path = Path(result["path"])
                entry = load_entry(entry_path)
                if cached_vector is not None:
                    # G5: reuse the embedding from the dedup check.
                    vi.upsert_with_vector(
                        entry, result["class"], entry_path,
                        precomputed_vector=cached_vector,
                    )
                else:
                    vi.upsert(entry, result["class"], entry_path)
            except Exception as exc:
                logger.warning("vector upsert failed for %s: %s", result["id"], exc)

        # Re-index FTS5 synchronously (doc 02 §6.2). Best-effort: a
        # failure here logs + continues so the markdown write still
        # succeeds. Reindex from the file path so the indexer derives
        # the BM25 text via the canonical text-composition rule.
        try:
            from durin.memory.indexer import reindex_one_file
            reindex_one_file(self._workspace, Path(result["path"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "memory_store FTS reindex failed for %s: %s",
                result["id"], exc,
            )

        # §8e: the per-entity threshold dream trigger is
        # removed — the daily extract/refine passes handle consolidation.
        return result
