"""Tests for memory telemetry aggregator (doc 25 §2.E)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from durin.memory.stats import (
    MemoryStats,
    compute_stats,
    derive_grep_total,
)


# ---------------------------------------------------------------------------
# Filesystem scan
# ---------------------------------------------------------------------------


def _write_entry(workspace: Path, class_name: str, entry_id: str,
                 entities: list[str] | None = None) -> Path:
    """Build a minimal episodic-style entry with the given entities list."""
    entries = workspace / "memory" / class_name
    entries.mkdir(parents=True, exist_ok=True)
    path = entries / f"{entry_id}.md"
    entities_repr = "[]" if not entities else "[" + ", ".join(entities) + "]"
    path.write_text(
        f"---\n"
        f"id: {entry_id}\n"
        f"class_name: {class_name}\n"
        f"entities: {entities_repr}\n"
        f"---\n\n"
        f"body\n",
        encoding="utf-8",
    )
    return path


def _write_entity_page(workspace: Path, type_: str, slug: str,
                       archived: bool = False) -> Path:
    """Build a minimal entity page on disk."""
    if archived:
        # archive lives at memory/entities/<type>/<canonical>/archive/<slug>.md
        # for the test we just need ONE pre-existing canonical to host it.
        base = workspace / "memory" / "entities" / type_ / "canonical" / "archive"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{slug}.md"
    else:
        base = workspace / "memory" / "entities" / type_
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{slug}.md"
    path.write_text(
        f"---\ntype: {type_}\nname: {slug}\n---\n\n# {slug}\n",
        encoding="utf-8",
    )
    return path


def test_filesystem_scan_counts_episodic_tagged_and_untagged(
    tmp_path: Path,
) -> None:
    _write_entry(tmp_path, "episodic", "e1", entities=["person:marcelo"])
    _write_entry(tmp_path, "episodic", "e2", entities=["topic:auth"])
    _write_entry(tmp_path, "episodic", "e3", entities=[])  # untagged
    # Different class_name should still count toward "on disk" but
    # filesystem scan only looks at episodic/ today.
    stats = compute_stats(tmp_path, telemetry_dir=tmp_path / "empty")
    assert stats.episodic_entries_on_disk == 3
    assert stats.episodic_entries_tagged == 2


def test_filesystem_scan_counts_entity_pages_and_archive(
    tmp_path: Path,
) -> None:
    _write_entity_page(tmp_path, "person", "marcelo")
    _write_entity_page(tmp_path, "project", "durin")
    _write_entity_page(tmp_path, "person", "marcelo_old", archived=True)

    stats = compute_stats(tmp_path, telemetry_dir=tmp_path / "empty")
    # canonical/ creates an empty parent page entry too — count it
    # accurately by walking. The fixture writes 2 canonicals + 1 archived
    # under a synthetic "canonical" subdir which we DON'T write a .md
    # for; so on-disk = 2, archived = 1.
    assert stats.entity_pages_on_disk == 2
    assert stats.entity_pages_archived == 1


def test_filesystem_scan_missing_memory_root_yields_zeros(
    tmp_path: Path,
) -> None:
    stats = compute_stats(tmp_path, telemetry_dir=tmp_path / "empty")
    assert stats.episodic_entries_on_disk == 0
    assert stats.episodic_entries_tagged == 0
    assert stats.entity_pages_on_disk == 0


# ---------------------------------------------------------------------------
# Telemetry scan
# ---------------------------------------------------------------------------


def _write_jsonl(
    telemetry_dir: Path,
    filename: str,
    events: list[dict],
) -> Path:
    """Build a synthetic telemetry file with the given events."""
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    path = telemetry_dir / filename
    with path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return path


def test_telemetry_recall_aggregation(tmp_path: Path) -> None:
    tel = tmp_path / "tel"
    now = time.time()
    _write_jsonl(tel, "s1_2026.jsonl", [
        {"ts": now, "type": "memory.recall",
         "data": {"query": "a", "scope": "all", "level": "warm",
                  "result_count": 5}},
        {"ts": now, "type": "memory.recall",
         "data": {"query": "b", "scope": "dreamed", "level": "warm",
                  "result_count": 2}},
        {"ts": now, "type": "memory.recall.vector",
         "data": {"query": "a", "scope": "all", "embedding_model": "m",
                  "hit_count": 5, "duration_ms": 30.0,
                  "ranking": "entity_aware", "query_entities_count": 1,
                  "reordered": True, "top_1_id_before": "x",
                  "top_1_id_after": "y"}},
        {"ts": now, "type": "memory.recall.vector",
         "data": {"query": "b", "scope": "dreamed", "embedding_model": "m",
                  "hit_count": 2, "duration_ms": 10.0,
                  "ranking": "default", "query_entities_count": 0,
                  "reordered": False, "top_1_id_before": "x",
                  "top_1_id_after": "x"}},
    ])
    stats = compute_stats(tmp_path, telemetry_dir=tel)
    assert stats.recall_total == 2
    assert stats.recall_vector_total == 2
    assert stats.recall_grep_total == 0  # both went through vector path
    assert stats.recall_vector_entity_aware == 1
    assert stats.recall_vector_reordered == 1
    assert stats.recall_vector_duration_ms_total == 40.0
    assert stats.recall_vector_hit_count_total == 7
    assert stats.entity_aware_ratio == 0.5
    assert stats.reordered_ratio == 0.5
    assert stats.vector_strategy_ratio == 1.0


def test_telemetry_grep_only_recall(tmp_path: Path) -> None:
    """recall fired but no vector event: that's grep fallback."""
    tel = tmp_path / "tel"
    now = time.time()
    _write_jsonl(tel, "s.jsonl", [
        {"ts": now, "type": "memory.recall",
         "data": {"query": "x", "scope": "undreamed", "level": "warm",
                  "result_count": 0}},
        {"ts": now, "type": "memory.recall",
         "data": {"query": "y", "scope": "all", "level": "cold",
                  "result_count": 1}},
    ])
    stats = compute_stats(tmp_path, telemetry_dir=tel)
    assert stats.recall_total == 2
    assert stats.recall_vector_total == 0
    assert stats.recall_grep_total == 2
    assert stats.vector_strategy_ratio == 0.0


def test_telemetry_store_and_blocked_duplicate(tmp_path: Path) -> None:
    tel = tmp_path / "tel"
    now = time.time()
    _write_jsonl(tel, "s.jsonl", [
        {"ts": now, "type": "memory.store",
         "data": {"entry_id": "e1", "class_name": "stable",
                  "author": "agent_created", "headline": "hello"}},
        {"ts": now, "type": "memory.store",
         "data": {"entry_id": "e2", "class_name": "stable",
                  "author": "agent_created", "headline": "world"}},
        {"ts": now, "type": "memory.store.blocked_near_duplicate",
         "data": {"candidate_class_name": "stable",
                  "existing_id": "e1", "existing_class_name": "stable",
                  "distance": 0.05, "threshold": 0.10}},
    ])
    stats = compute_stats(tmp_path, telemetry_dir=tel)
    assert stats.store_total == 2
    assert stats.store_blocked_near_duplicate == 1


def test_telemetry_days_filter_excludes_old_events(tmp_path: Path) -> None:
    tel = tmp_path / "tel"
    now = time.time()
    old = now - (10 * 86400)  # 10 days ago
    _write_jsonl(tel, "s.jsonl", [
        {"ts": old, "type": "memory.store",
         "data": {"entry_id": "old", "class_name": "stable",
                  "author": "agent_created", "headline": ""}},
        {"ts": now, "type": "memory.store",
         "data": {"entry_id": "new", "class_name": "stable",
                  "author": "agent_created", "headline": ""}},
    ])
    stats = compute_stats(tmp_path, telemetry_dir=tel, days=5)
    assert stats.store_total == 1


def test_telemetry_skips_non_memory_events(tmp_path: Path) -> None:
    tel = tmp_path / "tel"
    now = time.time()
    _write_jsonl(tel, "s.jsonl", [
        {"ts": now, "type": "agent_mode.turn_start", "data": {"mode": "build"}},
        {"ts": now, "type": "tool.read_file", "data": {"path": "a"}},
        {"ts": now, "type": "memory.store",
         "data": {"entry_id": "e1", "class_name": "stable",
                  "author": "agent_created", "headline": ""}},
    ])
    stats = compute_stats(tmp_path, telemetry_dir=tel)
    assert stats.telemetry_events_scanned == 1
    assert stats.store_total == 1


def test_telemetry_handles_corrupt_lines(tmp_path: Path) -> None:
    tel = tmp_path / "tel"
    tel.mkdir()
    (tel / "s.jsonl").write_text(
        "not json\n"
        '{"ts": 0, "type": "memory.store", "data": {"entry_id": "x", '
        '"class_name": "stable", "author": "agent_created", "headline": ""}}\n'
        '{"this is": "valid json but no type"}\n',
        encoding="utf-8",
    )
    stats = compute_stats(tmp_path, telemetry_dir=tel)
    assert stats.store_total == 1  # the one valid memory event


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------


def test_derive_grep_total_handles_vector_majority(tmp_path: Path) -> None:
    stats = MemoryStats()
    stats.recall_total = 10
    stats.recall_vector_total = 7
    derive_grep_total(stats)
    assert stats.recall_grep_total == 3


def test_derive_grep_total_clamps_to_zero_when_inconsistent(tmp_path: Path) -> None:
    """If telemetry is missing some recall events but kept vector events
    (e.g. partial corruption), vector_total may exceed recall_total.
    Clamp grep_total to 0 instead of going negative."""
    stats = MemoryStats()
    stats.recall_total = 3
    stats.recall_vector_total = 5
    derive_grep_total(stats)
    assert stats.recall_grep_total == 0


# ---------------------------------------------------------------------------
# Ratios on empty data
# ---------------------------------------------------------------------------


def test_ratios_safe_on_empty_stats() -> None:
    stats = MemoryStats()
    assert stats.reordered_ratio == 0.0
    assert stats.entity_aware_ratio == 0.0
    assert stats.vector_strategy_ratio == 0.0


# ---------------------------------------------------------------------------
# to_dict roundtrip
# ---------------------------------------------------------------------------


def test_to_dict_is_json_serialisable() -> None:
    stats = MemoryStats()
    stats.episodic_entries_on_disk = 3
    stats.recall_total = 2
    stats.recall_vector_total = 1
    derive_grep_total(stats)
    payload = stats.to_dict()
    # Roundtrip through JSON without errors.
    text = json.dumps(payload)
    assert json.loads(text) == payload
    assert payload["filesystem"]["episodic_entries_on_disk"] == 3
    assert payload["recall"]["grep_total"] == 1
