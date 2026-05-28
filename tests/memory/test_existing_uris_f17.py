"""F17 (audit third pass, 2026-05-28): wire `existing_uris` so the
Dream prompt sees the workspace's recent entity inventory.

Pre-F17 the slot rendered as an empty bulleted list regardless of
workspace state — the LLM had no signal about which entity URIs
already existed, so it could happily emit a `===PATCH===` that
creates `person:marcelo_marmol` even when `person:marcelo` was
already in `memory/entities/`.

F17 ships the producer documented in doc 06: walk
`memory/entities/`, sort by mtime descending, cap at 100, return
URIs as `<type>:<slug>`. Empty (no entities yet) and large
workspaces (>100) both pass through cleanly.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def _touch_mtime(path: Path, age_seconds: int) -> None:
    """Set both atime and mtime to `now - age_seconds`."""
    target = time.time() - age_seconds
    os.utime(path, (target, target))


def _make_entity(workspace: Path, type_: str, slug: str, age_seconds: int) -> None:
    p = workspace / "memory" / "entities" / type_ / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: {type_}\nname: {slug.replace('_', ' ').title()}\n---\n\nbody\n",
        encoding="utf-8",
    )
    _touch_mtime(p, age_seconds)


def test_empty_workspace_returns_empty_tuple(tmp_path: Path) -> None:
    from durin.memory.entity_inventory import existing_uris_by_recent_mtime

    assert existing_uris_by_recent_mtime(tmp_path) == ()


def test_collects_uris_under_entities(tmp_path: Path) -> None:
    from durin.memory.entity_inventory import existing_uris_by_recent_mtime

    _make_entity(tmp_path, "person", "marcelo", age_seconds=10)
    _make_entity(tmp_path, "project", "durin", age_seconds=20)
    _make_entity(tmp_path, "topic", "memory", age_seconds=30)

    uris = existing_uris_by_recent_mtime(tmp_path)
    assert set(uris) == {"person:marcelo", "project:durin", "topic:memory"}


def test_sorted_by_recent_mtime_descending(tmp_path: Path) -> None:
    """The most recently touched entity appears first — gives the LLM
    fresh context."""
    from durin.memory.entity_inventory import existing_uris_by_recent_mtime

    _make_entity(tmp_path, "person", "old_one", age_seconds=10_000)
    _make_entity(tmp_path, "person", "newer", age_seconds=100)
    _make_entity(tmp_path, "person", "newest", age_seconds=5)

    uris = existing_uris_by_recent_mtime(tmp_path)
    assert uris[0] == "person:newest"
    assert uris[1] == "person:newer"
    assert uris[2] == "person:old_one"


def test_caps_at_100_by_default(tmp_path: Path) -> None:
    from durin.memory.entity_inventory import existing_uris_by_recent_mtime

    # Seed 120 entities; the producer must drop everything past 100.
    for i in range(120):
        _make_entity(tmp_path, "person", f"person_{i:03d}", age_seconds=i)

    uris = existing_uris_by_recent_mtime(tmp_path)
    assert len(uris) == 100
    # The most recent (smallest age) survive; the dropped ones are
    # the OLDEST (age_seconds = 100..119).
    assert "person:person_000" in uris
    assert "person:person_099" in uris
    assert "person:person_119" not in uris


def test_custom_cap(tmp_path: Path) -> None:
    from durin.memory.entity_inventory import existing_uris_by_recent_mtime

    for i in range(10):
        _make_entity(tmp_path, "person", f"p_{i}", age_seconds=i)

    uris = existing_uris_by_recent_mtime(tmp_path, cap=3)
    assert len(uris) == 3


def test_excludes_archive(tmp_path: Path) -> None:
    """Archived entity pages live under `memory/archive/entities/...`
    or `memory/entities/<canonical>/archive/...`; both must NOT
    leak into the existing_uris signal."""
    from durin.memory.entity_inventory import existing_uris_by_recent_mtime

    _make_entity(tmp_path, "person", "marcelo", age_seconds=10)
    # Legacy nested archive
    nested_archive = (
        tmp_path / "memory" / "entities" / "person" / "marcelo"
        / "archive" / "old.md"
    )
    nested_archive.parent.mkdir(parents=True, exist_ok=True)
    nested_archive.write_text(
        "---\ntype: person\nname: Old\n---\nbody\n", encoding="utf-8",
    )
    # Top-level archive
    top_archive = (
        tmp_path / "memory" / "archive" / "entities" / "person" / "absorbed.md"
    )
    top_archive.parent.mkdir(parents=True, exist_ok=True)
    top_archive.write_text(
        "---\ntype: person\nname: Absorbed\n---\nbody\n", encoding="utf-8",
    )

    uris = existing_uris_by_recent_mtime(tmp_path)
    assert "person:marcelo" in uris
    # Neither archived variant surfaces.
    assert not any("old" in u or "absorbed" in u for u in uris)


def test_dream_prompt_now_carries_existing_uris(tmp_path: Path) -> None:
    """End-to-end: a workspace with entities → `DreamConsolidator
    ._build_prompt` populates the slot with the URI bullets."""
    from durin.memory.dream import DreamConsolidator

    _make_entity(tmp_path, "person", "marcelo", age_seconds=10)
    _make_entity(tmp_path, "project", "durin", age_seconds=20)

    c = DreamConsolidator(workspace=tmp_path)
    prompt = c._build_prompt(
        entity_ref="person:newperson",
        current_page=None,
        entries=[],
    )
    assert "person:marcelo" in prompt
    assert "project:durin" in prompt
