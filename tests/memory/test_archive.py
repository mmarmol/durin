"""Tests for archive helpers.

The archive convention moves consolidated content to `memory/archive/<class>/...`
with `archived_at` (and optional `archived_into`) frontmatter fields added.

The functions tested here are the canonical move operations. Dream apply and
absorption are their consumers.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _episodic(ws: Path, name: str, body: str = "body") -> Path:
    p = ws / "memory" / "episodic" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {name}\nheadline: {name} hl\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return p


def _entity(ws: Path, etype: str, slug: str, body: str = "body") -> Path:
    p = ws / "memory" / "entities" / etype / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: {etype}\nname: {slug.title()}\naliases: []\n"
        f"created_at: 2026-05-23T10:00:00\n"
        f"updated_at: 2026-05-23T10:00:00\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return p


def test_archive_episodic_moves_file(tmp_path: Path) -> None:
    """archive_episodic moves memory/episodic/<id>.md to
    memory/archive/episodic/<id>.md and removes the original."""
    from durin.memory.archive import archive_episodic

    src = _episodic(tmp_path, "2026-05-23T10-12-uuid")

    archive_episodic(tmp_path, src, into_uri="person:marcelo")

    expected = (
        tmp_path
        / "memory"
        / "archive"
        / "episodic"
        / "2026-05-23T10-12-uuid.md"
    )
    assert expected.exists()
    assert not src.exists()


def test_archive_episodic_annotates_frontmatter(tmp_path: Path) -> None:
    """archive_episodic injects `archived_at` and `archived_into` into frontmatter."""
    from durin.memory.archive import archive_episodic

    src = _episodic(tmp_path, "2026-05-23T10-12-uuid")
    dest = archive_episodic(tmp_path, src, into_uri="person:marcelo")

    content = dest.read_text(encoding="utf-8")
    assert "archived_into: person:marcelo" in content
    # `archived_at` timestamp should be a UTC ISO-8601 string. Sanity:
    # contains the year and a 'T' separator.
    assert "archived_at: '2026-" in content or 'archived_at: "2026-' in content or "archived_at: 2026-" in content
    assert "T" in content


def test_archive_episodic_returns_destination_path(tmp_path: Path) -> None:
    """Return value is the new absolute path under memory/archive/episodic/."""
    from durin.memory.archive import archive_episodic

    src = _episodic(tmp_path, "abc-123")
    dest = archive_episodic(tmp_path, src, into_uri="person:x")

    assert dest == tmp_path / "memory" / "archive" / "episodic" / "abc-123.md"


def test_archive_episodic_raises_on_missing_file(tmp_path: Path) -> None:
    """Missing source file raises FileNotFoundError (caller bug, fail loud)."""
    from durin.memory.archive import archive_episodic

    bogus = tmp_path / "memory" / "episodic" / "does-not-exist.md"

    with pytest.raises(FileNotFoundError):
        archive_episodic(tmp_path, bogus, into_uri="person:x")


def test_archive_episodic_raises_on_wrong_source_dir(tmp_path: Path) -> None:
    """Source must live under memory/episodic/ — refuse otherwise."""
    from durin.memory.archive import archive_episodic

    # File exists but under stable/ — not an episodic.
    stable = tmp_path / "memory" / "stable" / "promoted.md"
    stable.parent.mkdir(parents=True, exist_ok=True)
    stable.write_text("---\nid: x\nheadline: x\n---\n\n", encoding="utf-8")

    with pytest.raises(ValueError, match="episodic"):
        archive_episodic(tmp_path, stable, into_uri="person:x")


def test_archive_entity_moves_file(tmp_path: Path) -> None:
    """archive_entity moves memory/entities/<type>/<slug>.md to
    memory/archive/entities/<type>/<slug>.md and removes the original."""
    from durin.memory.archive import archive_entity

    src = _entity(tmp_path, "person", "marcelo-m")

    archive_entity(tmp_path, src, into_uri="person:marcelo")

    expected = (
        tmp_path
        / "memory"
        / "archive"
        / "entities"
        / "person"
        / "marcelo-m.md"
    )
    assert expected.exists()
    assert not src.exists()


def test_archive_entity_annotates_frontmatter(tmp_path: Path) -> None:
    """archive_entity injects `archived_at` and `archived_into` into frontmatter."""
    from durin.memory.archive import archive_entity

    src = _entity(tmp_path, "person", "marcelo-m")
    dest = archive_entity(tmp_path, src, into_uri="person:marcelo")

    content = dest.read_text(encoding="utf-8")
    assert "archived_into: person:marcelo" in content
    assert "archived_at:" in content


def test_archive_entity_returns_destination_path(tmp_path: Path) -> None:
    """Return value is the new absolute path under memory/archive/entities/<type>/."""
    from durin.memory.archive import archive_entity

    src = _entity(tmp_path, "person", "alice")
    dest = archive_entity(tmp_path, src, into_uri="person:bob")

    assert dest == tmp_path / "memory" / "archive" / "entities" / "person" / "alice.md"


def test_archive_entity_raises_on_missing_file(tmp_path: Path) -> None:
    from durin.memory.archive import archive_entity

    bogus = tmp_path / "memory" / "entities" / "person" / "missing.md"

    with pytest.raises(FileNotFoundError):
        archive_entity(tmp_path, bogus, into_uri="person:x")


def test_archive_entity_raises_on_wrong_source_dir(tmp_path: Path) -> None:
    """Source must live under memory/entities/ — refuse otherwise."""
    from durin.memory.archive import archive_entity

    rogue = tmp_path / "memory" / "episodic" / "not-an-entity.md"
    rogue.parent.mkdir(parents=True, exist_ok=True)
    rogue.write_text("---\nid: x\nheadline: x\n---\n\n", encoding="utf-8")

    with pytest.raises(ValueError, match="entity"):
        archive_entity(tmp_path, rogue, into_uri="person:x")


def test_archive_episodic_preserves_body(tmp_path: Path) -> None:
    """Body content survives the move unchanged."""
    from durin.memory.archive import archive_episodic

    src = _episodic(tmp_path, "test-id", body="Marcelo dijo X el lunes.")
    dest = archive_episodic(tmp_path, src, into_uri="person:marcelo")

    content = dest.read_text(encoding="utf-8")
    assert "Marcelo dijo X el lunes." in content


def test_archive_entity_with_reason(tmp_path: Path) -> None:
    """Optional `reason` is recorded as `archived_reason` in frontmatter
    so auditors can see why a merge happened (judge reasoning, manual
    operator note, etc.)."""
    from durin.memory.archive import archive_entity

    src = _entity(tmp_path, "person", "marcelo-m")
    dest = archive_entity(
        tmp_path,
        src,
        into_uri="person:marcelo",
        reason="alias overlap confirmed",
    )

    content = dest.read_text(encoding="utf-8")
    assert "archived_reason: alias overlap confirmed" in content


def test_archive_entity_no_reason_omits_field(tmp_path: Path) -> None:
    """Without `reason`, no `archived_reason` field appears."""
    from durin.memory.archive import archive_entity

    src = _entity(tmp_path, "person", "alice")
    dest = archive_entity(tmp_path, src, into_uri="person:bob")

    content = dest.read_text(encoding="utf-8")
    assert "archived_reason" not in content


def test_archive_episodic_with_reason(tmp_path: Path) -> None:
    """Episodic archive also supports `reason`."""
    from durin.memory.archive import archive_episodic

    src = _episodic(tmp_path, "obs-1")
    dest = archive_episodic(
        tmp_path,
        src,
        into_uri="person:marcelo",
        reason="consolidated into entity page",
    )

    content = dest.read_text(encoding="utf-8")
    assert "archived_reason: consolidated into entity page" in content


# ---------------------------------------------------------------------------
# archive_generic_entry — stable / corpus / session_summary classes
# ---------------------------------------------------------------------------


def _generic(ws: Path, klass: str, name: str, body: str = "body") -> Path:
    p = ws / "memory" / klass / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {name}\nheadline: {name} hl\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return p


def test_archive_generic_stable_moves_file(tmp_path: Path) -> None:
    """archive_generic_entry handles `stable/` entries."""
    from durin.memory.archive import archive_generic_entry

    src = _generic(tmp_path, "stable", "user-likes-dark-mode")
    dest = archive_generic_entry(tmp_path, src, reason="user_forget")

    assert not src.exists()
    expected = tmp_path / "memory" / "archive" / "stable" / "user-likes-dark-mode.md"
    assert dest == expected
    assert dest.exists()
    body = dest.read_text(encoding="utf-8")
    assert "archived_at:" in body
    assert "archived_reason: user_forget" in body
    # No "archived_into" key — generic forgets don't have a target.
    assert "archived_into" not in body


def test_archive_generic_corpus(tmp_path: Path) -> None:
    """archive_generic_entry handles `corpus/` entries."""
    from durin.memory.archive import archive_generic_entry

    src = _generic(tmp_path, "corpus", "doc-2026-05-31-abc")
    dest = archive_generic_entry(tmp_path, src)

    assert not src.exists()
    assert dest == tmp_path / "memory" / "archive" / "corpus" / "doc-2026-05-31-abc.md"
    assert dest.exists()


def test_archive_generic_session_summary(tmp_path: Path) -> None:
    """archive_generic_entry handles `session_summary/` entries."""
    from durin.memory.archive import archive_generic_entry

    src = _generic(tmp_path, "session_summary", "2026-05-31-uuid")
    dest = archive_generic_entry(tmp_path, src)

    assert not src.exists()
    assert dest.exists()


def test_archive_generic_rejects_episodic(tmp_path: Path) -> None:
    """archive_generic_entry refuses `episodic/` — caller must use archive_episodic."""
    from durin.memory.archive import archive_generic_entry

    src = _episodic(tmp_path, "obs-1")
    with pytest.raises(ValueError, match="unsupported class"):
        archive_generic_entry(tmp_path, src)
    assert src.exists(), "rejected paths must NOT be moved"


def test_archive_generic_rejects_entities(tmp_path: Path) -> None:
    """archive_generic_entry refuses `entities/` — caller must use archive_entity."""
    from durin.memory.archive import archive_generic_entry

    src = _entity(tmp_path, "person", "marcelo")
    with pytest.raises(ValueError, match="unsupported class"):
        archive_generic_entry(tmp_path, src)
    assert src.exists()


def test_archive_generic_raises_on_missing(tmp_path: Path) -> None:
    from durin.memory.archive import archive_generic_entry

    src = tmp_path / "memory" / "stable" / "ghost.md"
    with pytest.raises(FileNotFoundError):
        archive_generic_entry(tmp_path, src)


def test_archive_generic_raises_on_outside_workspace(tmp_path: Path) -> None:
    from durin.memory.archive import archive_generic_entry

    src = tmp_path / "random.md"
    src.write_text("---\nid: x\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not under"):
        archive_generic_entry(tmp_path, src)
