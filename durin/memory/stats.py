"""Memory telemetry aggregator.

Reads JSONL events from ``~/.cache/durin/telemetry/`` and walks the
workspace filesystem for ground-truth counts. Read-only — never mutates
state. Per ``docs/25_post_t1_state_and_t2_horizon.md`` §2.E this is the
prerequisite for the §2.A / §2.D / §2.F / §2.G gates: each one is an
observable metric and without aggregation those gates are faith-based.

Used by ``durin memory stats`` (see ``cli/memory_cmd.py``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TELEMETRY_DIR = Path.home() / ".cache" / "durin" / "telemetry"

# Events the aggregator cares about. Other event types are skipped so
# we don't waste cycles parsing irrelevant lines.
_MEMORY_EVENT_PREFIXES = ("memory.",)


@dataclass
class MemoryStats:
    """Aggregated metrics over a time window.

    All counters default to 0 so callers can compare empty corpora.
    """

    # Time window applied (inclusive). ``None`` = no filter.
    since: datetime | None = None

    # Filesystem (ground truth, not event-derived)
    episodic_entries_on_disk: int = 0
    episodic_entries_tagged: int = 0
    entity_pages_on_disk: int = 0
    entity_pages_archived: int = 0

    # Recall events
    recall_total: int = 0
    recall_vector_total: int = 0
    recall_grep_total: int = 0
    recall_vector_entity_aware: int = 0
    recall_vector_reordered: int = 0
    recall_vector_duration_ms_total: float = 0.0
    recall_vector_hit_count_total: int = 0

    # Store events
    store_total: int = 0
    store_blocked_near_duplicate: int = 0

    # Ingest events
    ingest_total: int = 0
    ingest_bytes_total: int = 0

    # Embedding events
    embedding_load_count: int = 0
    embedding_load_duration_ms_total: float = 0.0
    embedding_embed_count: int = 0
    embedding_embed_duration_ms_total: float = 0.0
    embedding_embed_batch_size_total: int = 0

    # Files scanned (for diagnostics)
    telemetry_files_scanned: int = 0
    telemetry_events_scanned: int = 0

    @property
    def reordered_ratio(self) -> float:
        """Share of vector recalls where entity-aware reranking changed
        the top-1 result. Validates that the ranker is actually doing
        useful work (>0 means the entity match shifted the result;
        0 means the vector ordering was already correct).
        """
        if self.recall_vector_total == 0:
            return 0.0
        return self.recall_vector_reordered / self.recall_vector_total

    @property
    def entity_aware_ratio(self) -> float:
        """Share of vector recalls that activated entity-aware ranking
        (i.e. the query matched a known alias/identifier). Low ratio
        means queries don't reference known entities — vector path alone
        carries retrieval.
        """
        if self.recall_vector_total == 0:
            return 0.0
        return self.recall_vector_entity_aware / self.recall_vector_total

    @property
    def vector_strategy_ratio(self) -> float:
        """Share of recalls that took the vector path (vs grep fallback)."""
        if self.recall_total == 0:
            return 0.0
        return self.recall_vector_total / self.recall_total

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable representation."""
        return {
            "since": self.since.isoformat() if self.since else None,
            "filesystem": {
                "episodic_entries_on_disk": self.episodic_entries_on_disk,
                "episodic_entries_tagged": self.episodic_entries_tagged,
                "entity_pages_on_disk": self.entity_pages_on_disk,
                "entity_pages_archived": self.entity_pages_archived,
            },
            "recall": {
                "total": self.recall_total,
                "vector_total": self.recall_vector_total,
                "grep_total": self.recall_grep_total,
                "vector_entity_aware": self.recall_vector_entity_aware,
                "vector_reordered": self.recall_vector_reordered,
                "vector_duration_ms_total": self.recall_vector_duration_ms_total,
                "vector_hit_count_total": self.recall_vector_hit_count_total,
                "reordered_ratio": self.reordered_ratio,
                "entity_aware_ratio": self.entity_aware_ratio,
                "vector_strategy_ratio": self.vector_strategy_ratio,
            },
            "store": {
                "total": self.store_total,
                "blocked_near_duplicate": self.store_blocked_near_duplicate,
            },
            "ingest": {
                "total": self.ingest_total,
                "bytes_total": self.ingest_bytes_total,
            },
            "embedding": {
                "load_count": self.embedding_load_count,
                "load_duration_ms_total": self.embedding_load_duration_ms_total,
                "embed_count": self.embedding_embed_count,
                "embed_duration_ms_total": self.embedding_embed_duration_ms_total,
                "embed_batch_size_total": self.embedding_embed_batch_size_total,
            },
            "diagnostics": {
                "telemetry_files_scanned": self.telemetry_files_scanned,
                "telemetry_events_scanned": self.telemetry_events_scanned,
            },
        }


def compute_stats(
    workspace: Path,
    *,
    telemetry_dir: Path | None = None,
    days: int | None = None,
) -> MemoryStats:
    """Aggregate memory telemetry + walk workspace for ground truth.

    Parameters
    ----------
    workspace
        Absolute path to the durin workspace (``cfg.workspace_path``).
        Used to walk ``memory/episodic/`` and ``memory/entities/`` for
        ground-truth counts.
    telemetry_dir
        Where the JSONL event log lives. Defaults to
        ``~/.cache/durin/telemetry/``. Overridable for tests.
    days
        If set, only consider events whose ``ts`` is within the last
        ``days`` days. ``None`` = include everything.

    Returns
    -------
    MemoryStats
        Counters + derived ratios. Always returns a populated object;
        on missing directories the counters stay at zero.
    """
    if telemetry_dir is None:
        telemetry_dir = DEFAULT_TELEMETRY_DIR

    since: datetime | None = None
    since_ts: float | None = None
    if days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        since_ts = since.timestamp()

    stats = MemoryStats(since=since)

    _scan_filesystem(workspace, stats)
    _scan_telemetry(telemetry_dir, since_ts, stats)
    derive_grep_total(stats)
    return stats


def _scan_filesystem(workspace: Path, stats: MemoryStats) -> None:
    """Ground-truth counts from disk. Events can be lost / cleared;
    files are authoritative.
    """
    memory_root = workspace / "memory"
    if not memory_root.exists():
        return

    episodic = memory_root / "episodic"
    if episodic.exists():
        for md in episodic.rglob("*.md"):
            stats.episodic_entries_on_disk += 1
            if _entry_has_entities(md):
                stats.episodic_entries_tagged += 1

    entities = memory_root / "entities"
    if entities.exists():
        for md in entities.rglob("*.md"):
            # Pages under <slug>/archive/ are absorbed; track separately
            # so the gate metric for §2.D ("duplicates absorbed") is
            # observable from disk too, not only from telemetry.
            if "/archive/" in str(md):
                stats.entity_pages_archived += 1
            else:
                stats.entity_pages_on_disk += 1


def _entry_has_entities(md_path: Path) -> bool:
    """Quick frontmatter check: does the entry declare any entities?

    Cheap parse — we only need to know whether the field is non-empty.
    Falls back to False on read errors (corrupt entries don't count).
    """
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 4)
    if end < 0:
        return False
    frontmatter = text[3:end]
    # Match any non-empty entities list. Empty `entities: []` or
    # `entities:` (with no items) counts as untagged.
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if stripped.startswith("entities:"):
            rest = stripped[len("entities:"):].strip()
            if rest in ("", "[]"):
                return False
            # `entities: [foo]` inline OR `entities:\n  - foo` block.
            return True
    return False


def _scan_telemetry(
    telemetry_dir: Path,
    since_ts: float | None,
    stats: MemoryStats,
) -> None:
    """Walk JSONL files, dispatch memory.* events to the counters."""
    if not telemetry_dir.exists():
        return

    for jsonl_path in sorted(telemetry_dir.glob("*.jsonl")):
        stats.telemetry_files_scanned += 1
        try:
            with jsonl_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue

                    ts = event.get("ts")
                    if since_ts is not None and isinstance(ts, (int, float)):
                        if ts < since_ts:
                            continue

                    etype = event.get("type", "")
                    if not isinstance(etype, str):
                        continue
                    if not any(etype.startswith(p) for p in _MEMORY_EVENT_PREFIXES):
                        continue

                    stats.telemetry_events_scanned += 1
                    data = event.get("data", {})
                    if not isinstance(data, dict):
                        data = {}
                    _apply_event(etype, data, stats)
        except OSError as exc:
            logger.warning("memory.stats: skip unreadable %s: %s",
                           jsonl_path, exc)
            continue


def _apply_event(etype: str, data: dict[str, Any], stats: MemoryStats) -> None:
    """Update counters for one event. Unknown memory.* types are ignored
    (forward-compat: a new event won't break old aggregators)."""
    if etype == "memory.recall":
        stats.recall_total += 1
        # The recall event doesn't carry strategy directly; derive from
        # the vector event count after the loop. For now bucket "grep"
        # as "recalls that didn't fire a vector event". The vector path
        # ALWAYS emits both events, so vector_total ≤ recall_total.
    elif etype == "memory.recall.vector":
        stats.recall_vector_total += 1
        if data.get("ranking") == "entity_aware":
            stats.recall_vector_entity_aware += 1
        if data.get("reordered") is True:
            stats.recall_vector_reordered += 1
        duration = data.get("duration_ms")
        if isinstance(duration, (int, float)):
            stats.recall_vector_duration_ms_total += float(duration)
        hits = data.get("hit_count")
        if isinstance(hits, int):
            stats.recall_vector_hit_count_total += hits
    elif etype == "memory.store":
        stats.store_total += 1
    elif etype == "memory.store.blocked_near_duplicate":
        stats.store_blocked_near_duplicate += 1
    elif etype == "memory.ingest":
        stats.ingest_total += 1
        size = data.get("size_bytes")
        if isinstance(size, int):
            stats.ingest_bytes_total += size
    elif etype == "memory.embedding.load":
        stats.embedding_load_count += 1
        duration = data.get("duration_ms")
        if isinstance(duration, (int, float)):
            stats.embedding_load_duration_ms_total += float(duration)
    elif etype == "memory.embedding.embed":
        stats.embedding_embed_count += 1
        duration = data.get("duration_ms")
        if isinstance(duration, (int, float)):
            stats.embedding_embed_duration_ms_total += float(duration)
        bs = data.get("batch_size")
        if isinstance(bs, int):
            stats.embedding_embed_batch_size_total += bs


def derive_grep_total(stats: MemoryStats) -> None:
    """memory.recall fires for both paths; memory.recall.vector only for
    vector. Therefore grep_total = recall_total - vector_total.

    Called by ``compute_stats`` after the scan. Exposed separately so
    tests can verify the derivation independently of the scan logic.
    """
    stats.recall_grep_total = max(0, stats.recall_total - stats.recall_vector_total)
