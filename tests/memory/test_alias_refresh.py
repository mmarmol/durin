"""Regression: write_entity must refresh the shared AliasIndex in-process.

Hazard #17 (docs/architecture/concurrency.md): the shared AliasIndex
is built lazily and then mutated incrementally.  Before this fix,
write_entity did NOT call refresh_for on the shared index, so a
freshly written entity was invisible to entity-aware ranking until the
process restarted.

This test verifies the in-process fix: build the shared index, write a
new entity via write_entity, and assert the new entity's alias is
immediately visible via get_shared_alias_index without a restart.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from durin.memory.aliases_cache import _clear_all, get_shared_alias_index
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.provenance import author_scope

_NOW = datetime(2026, 6, 21, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_index_cache():
    """Ensure each test starts with a clean alias cache (no carryover)."""
    _clear_all()
    yield
    _clear_all()


def test_write_entity_refreshes_shared_alias_index(tmp_path: Path) -> None:
    """After write_entity, the shared AliasIndex must reflect the new entity.

    Steps:
    1. Build the shared alias index (primes the cache).
    2. Confirm the entity is absent.
    3. write_entity creates the entity with a display name.
    4. Query the SAME shared index — new entity must now be visible.
    """
    ws = tmp_path
    memory_root = ws / "memory"

    # Step 1: prime the shared alias index (triggers the lazy build).
    idx = get_shared_alias_index(memory_root)
    assert idx.lookup("acme corp") == []

    # Step 2: write a new entity with a recognisable name.
    with author_scope("agent_created"):
        write_entity(
            ws,
            "company:acme",
            [
                FieldPatch(
                    kind="body_append",
                    value="Seed body.",
                    author="agent",
                    source_ref="[[sessions/s.md#turn-0]]",
                    at=_NOW,
                )
            ],
            create=True,
            name="Acme Corp",
        )

    # Step 3: query the shared index — must see the new entity without rebuild.
    idx2 = get_shared_alias_index(memory_root)
    assert idx2 is idx, "get_shared_alias_index must return the same instance"
    hits = idx2.lookup("acme corp")
    assert "company:acme" in hits, (
        f"Expected company:acme in alias index after write_entity, got: {hits}"
    )


def test_write_entity_refresh_updates_existing_entity_aliases(tmp_path: Path) -> None:
    """Adding an alias via write_entity must update the shared index incrementally."""
    ws = tmp_path
    memory_root = ws / "memory"

    with author_scope("agent_created"):
        write_entity(
            ws,
            "person:bob",
            [FieldPatch(kind="body_append", value="hi", author="agent",
                        source_ref="s", at=_NOW)],
            create=True,
            name="Bob",
        )

    # Prime index AFTER the first write (entity already on disk).
    idx = get_shared_alias_index(memory_root)
    assert "person:bob" in idx.lookup("bob")
    assert idx.lookup("robert") == []

    # Now add an alias via write_entity.
    with author_scope("agent_created"):
        write_entity(
            ws,
            "person:bob",
            [FieldPatch(kind="alias", value="Robert", author="agent",
                        source_ref="s2", at=_NOW)],
        )

    # The shared index must reflect the new alias immediately.
    assert "person:bob" in idx.lookup("robert"), (
        "Alias 'robert' must appear in shared index after write_entity adds it"
    )


def test_write_entity_noop_does_not_break_index(tmp_path: Path) -> None:
    """A no-op write (duplicate alias → WriteResult.committed=False) must not corrupt the index."""
    ws = tmp_path
    memory_root = ws / "memory"

    with author_scope("agent_created"):
        write_entity(
            ws,
            "topic:foo",
            [FieldPatch(kind="alias", value="Foo Alias", author="agent",
                        source_ref="s", at=_NOW)],
            create=True,
            name="Foo",
        )

    idx = get_shared_alias_index(memory_root)
    assert "topic:foo" in idx.lookup("foo alias")

    # Re-apply the same alias — aliases are de-duped so this is a no-op.
    with author_scope("agent_created"):
        result = write_entity(
            ws,
            "topic:foo",
            [FieldPatch(kind="alias", value="Foo Alias", author="agent",
                        source_ref="s", at=_NOW)],
        )
    assert not result.committed  # confirmed no-op

    # Index must still return the entity and not have grown stale.
    assert "topic:foo" in idx.lookup("foo alias")
