"""Telemetry for skill retrieval: count skill recalls + emit a skill-miss
signal.

Three behaviours:

(a) stats roll-up — synthetic ``memory.recall`` events carry a
    ``skill_result_count``; ``memory.skill_miss`` events bump a miss
    counter. Both surface in ``MemoryStats.to_dict()``.
(b) the real ``memory_search`` tool stamps ``skill_result_count`` on its
    ``memory.recall`` event when a query hits an indexed skill.
(c) a ``kinds="skill"`` query that surfaces nothing emits
    ``memory.skill_miss`` with ``had_skill_candidate=True`` when a skill
    DOES exist on disk (a real silent miss worth investigating).

The live-emit capture mirrors ``tests/agent/test_cache_usage_telemetry.py``:
a ``_RecordingLogger`` with a ``.log()`` method bound via
``current_telemetry()`` (monkeypatched on the module ``emit_tool_event``
imports it from). The skill fixture mirrors
``tests/agent/test_memory_search_skill.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from durin.agent.tools.memory_search import MemorySearchTool
from durin.memory.indexer import rebuild_fts_index

SKILL_NAME = "git-rebase-helper"
SKILL_NEEDLE = "uniqueskilltoken"


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_logger(monkeypatch, logger) -> None:
    """Stub current_telemetry() in the module emit_tool_event reads it from."""
    from durin.agent.tools import _telemetry
    monkeypatch.setattr(_telemetry, "current_telemetry", lambda: logger)


def _seed_skill(workspace: Path) -> None:
    skill_dir = workspace / "skills" / SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {SKILL_NAME}\n"
        f"description: how to {SKILL_NEEDLE} an interactive rebase\n"
        "---\n"
        f"Step 1: run git rebase -i to {SKILL_NEEDLE}.\n"
        "Step 2: reorder the commits.\n",
        encoding="utf-8",
    )
    rebuild_fts_index(workspace)


def test_stats_count_skill_recalls_and_misses():
    from durin.memory.stats import MemoryStats, _apply_event

    s = MemoryStats()
    _apply_event("memory.recall", {"skill_result_count": 2}, s)
    _apply_event("memory.recall", {"skill_result_count": 0}, s)
    _apply_event(
        "memory.skill_miss",
        {"query": "x", "result_count": 0, "had_skill_candidate": True},
        s,
    )

    assert s.recall_skill_total == 2
    assert s.skill_miss_total == 1

    d = s.to_dict()
    assert d["recall"]["skill_total"] == 2
    assert d["recall"]["skill_miss_total"] == 1


def test_recall_event_carries_skill_result_count(tmp_path: Path, monkeypatch):
    _seed_skill(tmp_path)
    logger = _RecordingLogger()
    _bind_logger(monkeypatch, logger)

    tool = MemorySearchTool(workspace=tmp_path)
    asyncio.run(tool.execute(query=SKILL_NEEDLE))

    recalls = [d for et, d in logger.events if et == "memory.recall"]
    assert recalls, f"no memory.recall emitted: {logger.events!r}"
    assert recalls[0]["skill_result_count"] >= 1, (
        f"skill_result_count not >=1: {recalls[0]!r}"
    )


def test_skill_miss_emitted_when_query_misses(tmp_path: Path, monkeypatch):
    # A skill EXISTS on disk, but the query matches nothing → a real
    # silent miss (had_skill_candidate is True).
    _seed_skill(tmp_path)
    logger = _RecordingLogger()
    _bind_logger(monkeypatch, logger)

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="zzznomatchquery", kinds="skill"),
    )
    assert not out["results"], f"expected empty result set, got {out!r}"

    misses = [d for et, d in logger.events if et == "memory.skill_miss"]
    assert len(misses) == 1, f"expected 1 skill_miss, got {logger.events!r}"
    assert misses[0]["result_count"] == 0
    assert misses[0]["had_skill_candidate"] is True
