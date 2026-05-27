"""`author: user_authored` entries are protected from Dream consumption.

Per `docs/memory/01_data_and_entities.md` §4.6.1:

A `user_authored` entry is intentional, durable, and human-curated.
Dream must NOT consolidate it (would compete with the user's choice
of canonical truth) and absorb-judge must NOT propose merging an
entity page whose authoring entry has `author: user_authored`.

This module-test pair targets the discovery helper used by both
trigger flows (`memory_cmd._discover_pending_consolidations`).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from durin.cli.memory_cmd import _discover_pending_consolidations
from durin.memory.schema import MemoryEntry
from durin.memory.storage import save_entry


def _episodic(
    workspace: Path,
    name: str,
    *,
    entities: list[str],
    author: str = "agent_created",
    valid_from: str = "2026-05-23",
) -> Path:
    """Write one episodic entry. ``author`` defaults to the standard
    agent-authored value."""
    path = workspace / "memory" / "episodic" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = MemoryEntry(
        id=name,
        headline=f"headline for {name}",
        entities=entities,
        author=author,  # type: ignore[arg-type]
        valid_from=date.fromisoformat(valid_from),
        body="body",
    )
    save_entry(entry, path)
    return path


# ---------------------------------------------------------------------------
# Filter behavior
# ---------------------------------------------------------------------------


def test_agent_created_entries_visible(tmp_path: Path) -> None:
    """Baseline: a normal agent-authored entry IS picked up."""
    _episodic(tmp_path, "e1", entities=["person:marcelo"])
    pending = _discover_pending_consolidations(tmp_path / "memory")
    assert "person:marcelo" in pending
    assert len(pending["person:marcelo"]) == 1


def test_user_authored_entries_skipped(tmp_path: Path) -> None:
    _episodic(
        tmp_path, "u1",
        entities=["person:marcelo"],
        author="user_authored",
    )
    pending = _discover_pending_consolidations(tmp_path / "memory")
    assert "person:marcelo" not in pending


def test_mixed_only_agent_created_returned(tmp_path: Path) -> None:
    _episodic(tmp_path, "a1", entities=["person:marcelo"])
    _episodic(
        tmp_path, "u1",
        entities=["person:marcelo"],
        author="user_authored",
    )
    _episodic(tmp_path, "a2", entities=["person:marcelo"])
    pending = _discover_pending_consolidations(tmp_path / "memory")
    ids = {entry.id for entry in pending["person:marcelo"]}
    assert ids == {"a1", "a2"}, (
        "user_authored entry leaked into pending consolidation set"
    )


def test_user_authored_does_not_block_unrelated_entity(
    tmp_path: Path,
) -> None:
    """user_authored entry tagged ONLY with person:m must not affect
    project:durin's pending list."""
    _episodic(
        tmp_path, "u1",
        entities=["person:marcelo"],
        author="user_authored",
    )
    _episodic(tmp_path, "p1", entities=["project:durin"])
    pending = _discover_pending_consolidations(tmp_path / "memory")
    assert "project:durin" in pending
    assert "person:marcelo" not in pending


def test_filter_works_with_entity_filter(tmp_path: Path) -> None:
    """When `entity_filter` is set, the user_authored skip still applies."""
    _episodic(
        tmp_path, "u1",
        entities=["person:marcelo"],
        author="user_authored",
    )
    pending = _discover_pending_consolidations(
        tmp_path / "memory", entity_filter="person:marcelo",
    )
    assert pending.get("person:marcelo", []) == []
