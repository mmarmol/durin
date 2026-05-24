"""memory_store tool — write a memory entry under memory/<class>/<id>.md."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import logging
from typing import Optional

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import (
    ArraySchema, BooleanSchema, StringSchema, tool_parameters_schema,
)
from durin.memory.paths import MEMORY_CLASSES
from durin.memory.provenance import author_scope
from durin.memory.store import StoreError, store_memory
from durin.memory.storage import load_entry
from durin.memory.vector_index import VectorIndex, vector_index_available

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    content=StringSchema(
        "Markdown body of the memory entry — the full text to remember."
    ),
    class_name=StringSchema(
        "Memory class. Default: episodic. "
        "stable=identity/corrections, episodic=working/recent, "
        "corpus=queryable archive, pending=prospective.",
        enum=list(MEMORY_CLASSES),
    ),
    headline=StringSchema(
        "Optional ~10-word headline. Auto-generated from content if omitted."
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
        "Persist a memory entry under memory/<class>/<id>.md. Author is "
        "stamped automatically from the agent's current write-origin "
        "(agent_created when called by the agent, user_authored otherwise). "
        "By default, content that's a near-duplicate of an existing entry "
        "(cosine sim >= 0.95) returns a warning instead of writing; pass "
        "force=true to override."
    ),
)


@tool_parameters(_PARAMETERS)
class MemoryStoreTool(Tool):
    """memory_store tool — persist distilled learnings as memory entries."""

    config_key = "memory"

    def __init__(
        self,
        workspace: str | Path,
        embedding_model: str | None = None,
        dream_config: Any | None = None,
    ) -> None:
        self._workspace = Path(workspace).expanduser()
        self._embedding_model = embedding_model
        # Lazily constructed once on first use; None means "disabled".
        self._vector_index: Optional[VectorIndex] = None
        self._vector_index_attempted = False
        # Doc 25 §2.A.1 β.2 — per-entity threshold trigger config. When
        # set + enabled + threshold_entries > 0, a successful write
        # checks the per-entity post-cursor count and may dispatch a
        # background dream pass. None disables the trigger entirely
        # (tests, environments without the config).
        self._dream_config = dream_config

    @property
    def name(self) -> str:
        return "memory_store"

    @property
    def description(self) -> str:
        return (
            "Persist a memory entry under memory/<class>/<id>.md with full "
            "frontmatter (headline + summary + body + source_refs + entities + "
            "author + valid_from). Idempotent: same (class, content) writes "
            "the same id. Author defaults to agent_created when invoked by "
            "the agent."
        )

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
        source_refs = kwargs.get("source_refs") or []
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

        # Doc 25 §2.A.1 β.2: threshold trigger. After a successful write
        # that tagged at least one entity, check whether any of those
        # entities crossed the per-entity post-cursor threshold. If yes,
        # dispatch a background dream pass (throttled by DreamRunner).
        # Fire-and-forget — must NOT block the tool response. Failures
        # are logged, never propagated.
        if entities:
            self._maybe_dispatch_threshold_dream(list(entities), vi)

        return result

    def _maybe_dispatch_threshold_dream(
        self,
        entities: list[str],
        vector_index: Optional[VectorIndex],
    ) -> None:
        """Per-entity threshold check + background dispatch (§2.A.1 β.2).

        For each entity ref in the just-written entry, count the
        post-cursor entries (reusing the same discovery helper the CLI
        and runner use). When any entity crosses ``threshold_entries``,
        spawn a daemon thread that invokes
        :meth:`DreamRunner.run` with ``trigger="threshold"`` and
        ``entity_filter=ref``. The runner's own throttle prevents
        thrashing when multiple thresholds fire in quick succession.
        """
        cfg = self._dream_config
        if cfg is None or not getattr(cfg, "enabled", False):
            return
        threshold = getattr(cfg, "threshold_entries", 0) or 0
        if threshold <= 0:
            return

        try:
            from durin.cli.memory_cmd import _discover_pending_consolidations

            memory_root = self._workspace / "memory"
            pending = _discover_pending_consolidations(
                memory_root, entity_filter=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("threshold dream: discover failed: %s", exc)
            return

        triggered_for: list[str] = []
        for ref in entities:
            entries = pending.get(ref) or []
            if len(entries) >= threshold:
                triggered_for.append(ref)

        if not triggered_for:
            return

        # Spawn one daemon thread per triggered entity. The runner
        # serialises with its own lock so two threads can't double-
        # consolidate the same workspace.
        import threading

        from durin.memory.dream_runner import DreamRunner

        for ref in triggered_for:
            def _run(entity_ref: str = ref) -> None:
                try:
                    auto_cfg = getattr(cfg, "auto_absorb", None)
                    runner = DreamRunner(
                        workspace=self._workspace,
                        min_seconds_between_runs=getattr(
                            cfg, "min_seconds_between_runs", 300,
                        ),
                        model=getattr(cfg, "model_override", None),
                        vector_index=vector_index,
                        auto_absorb_enabled=bool(
                            getattr(auto_cfg, "enabled", False),
                        ),
                        auto_absorb_threshold=int(
                            getattr(auto_cfg, "confidence_threshold", 95),
                        ),
                        auto_absorb_min_age_hours=int(
                            getattr(auto_cfg, "min_age_hours", 24),
                        ),
                        auto_absorb_judge_model=getattr(
                            auto_cfg, "judge_model", None,
                        ),
                    )
                    runner.run(trigger="threshold", entity_filter=entity_ref)
                except Exception:
                    logger.exception(
                        "threshold dream for %s failed", entity_ref,
                    )

            t = threading.Thread(target=_run, daemon=True,
                                 name=f"dream-threshold-{ref}")
            t.start()
