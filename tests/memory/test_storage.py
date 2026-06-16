"""Tests for MemoryEntry schema validation and on-disk round-trip."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from durin.memory.schema import MemoryEntry
from durin.memory.storage import (
    FrontmatterError,
    load_entry,
    save_entry,
    split_frontmatter,
)

# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_minimal_entry_only_requires_id_and_headline() -> None:
    entry = MemoryEntry(id="mem-001", headline="terse, no emojis")
    assert entry.id == "mem-001"
    assert entry.headline == "terse, no emojis"
    assert entry.summary == ""
    assert entry.body == ""
    assert entry.source_refs == []
    assert entry.related == []
    assert entry.entities == []
    assert entry.author == "user_authored"
    assert entry.valid_from is None


def test_missing_id_raises() -> None:
    with pytest.raises(ValidationError):
        MemoryEntry(headline="x")  # type: ignore[call-arg]


def test_missing_headline_raises() -> None:
    with pytest.raises(ValidationError):
        MemoryEntry(id="mem-001")  # type: ignore[call-arg]


def test_author_accepts_agent_created() -> None:
    entry = MemoryEntry(id="mem-001", headline="x", author="agent_created")
    assert entry.author == "agent_created"


def test_author_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        MemoryEntry(id="mem-001", headline="x", author="other")  # type: ignore[arg-type]


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryEntry(id="mem-001", headline="x", unknown_field="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_minimal_entry(tmp_path: Path) -> None:
    entry = MemoryEntry(id="mem-001", headline="terse")
    path = tmp_path / "mem-001.md"
    save_entry(entry, path)
    loaded = load_entry(path)
    assert loaded == entry


def test_round_trip_full_entry(tmp_path: Path) -> None:
    entry = MemoryEntry(
        id="mem-001",
        headline="User prefiere terse, sin emojis",
        summary="Confirmado en S1, refinado en S3 tras corrección",
        source_refs=[
            "[turn 42](../sessions/abc.md#turn-42)",
            "[seccion 3.1](../ingested/doc-7/source.md#api-conventions)",
        ],
        related=["[refina](mem-001-prev)"],
        entities=["usuario:marcelo", "proyecto:durin"],
        author="agent_created",
        valid_from=date(2026, 5, 20),
        body="Detalle: el usuario corrigió en S5 que emojis solo si los pide explícitamente.",
    )
    path = tmp_path / "mem-001.md"
    save_entry(entry, path)
    loaded = load_entry(path)
    assert loaded == entry


def test_round_trip_preserves_multiline_body(tmp_path: Path) -> None:
    body = (
        "Primer párrafo.\n"
        "\n"
        "Segundo párrafo con `code` y [link](http://x.com).\n"
        "\n"
        "- bullet\n"
        "- otro"
    )
    entry = MemoryEntry(id="mem-001", headline="h", body=body)
    path = tmp_path / "mem-001.md"
    save_entry(entry, path)
    loaded = load_entry(path)
    assert loaded.body == body


def test_round_trip_preserves_unicode(tmp_path: Path) -> None:
    entry = MemoryEntry(
        id="mem-001",
        headline="日本語のヘッドライン",
        body="Contenido con ñ, á, é, í — y emoji 🎯",
    )
    path = tmp_path / "mem-001.md"
    save_entry(entry, path)
    loaded = load_entry(path)
    assert loaded == entry


def test_round_trip_empty_body(tmp_path: Path) -> None:
    entry = MemoryEntry(id="mem-001", headline="h", body="")
    path = tmp_path / "mem-001.md"
    save_entry(entry, path)
    loaded = load_entry(path)
    assert loaded.body == ""


def test_round_trip_preserves_list_order(tmp_path: Path) -> None:
    entry = MemoryEntry(
        id="mem-001",
        headline="h",
        source_refs=["[a](a.md)", "[b](b.md)", "[c](c.md)"],
        entities=["topic:z_entity", "topic:a_entity", "topic:m_entity"],
    )
    path = tmp_path / "mem-001.md"
    save_entry(entry, path)
    loaded = load_entry(path)
    assert loaded.source_refs == ["[a](a.md)", "[b](b.md)", "[c](c.md)"]
    assert loaded.entities == ["topic:z_entity", "topic:a_entity", "topic:m_entity"]


# ---------------------------------------------------------------------------
# Frontmatter parsing errors
# ---------------------------------------------------------------------------


def test_split_frontmatter_missing_leading_delimiter() -> None:
    with pytest.raises(FrontmatterError, match="leading"):
        split_frontmatter("no delimiter here\n")


def test_split_frontmatter_unclosed() -> None:
    with pytest.raises(FrontmatterError, match="unclosed"):
        split_frontmatter("---\nid: x\nheadline: y\n")


def test_split_frontmatter_malformed_yaml() -> None:
    with pytest.raises(FrontmatterError, match="malformed YAML"):
        split_frontmatter("---\nid: [unclosed\n---\n\nbody")


def test_split_frontmatter_non_dict() -> None:
    with pytest.raises(FrontmatterError, match="mapping"):
        split_frontmatter("---\n- 1\n- 2\n---\n\nbody")


def test_load_entry_schema_violation_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.md"
    path.write_text("---\nid: mem-001\n---\n\nbody\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_entry(path)


# ---------------------------------------------------------------------------
# P9 Cambio 3: wikilink (Obsidian-style) serialization of source_refs / related
# ---------------------------------------------------------------------------


def test_save_entry_serializes_refs_as_wikilinks(tmp_path: Path) -> None:
    """source_refs and related must be written as Obsidian wikilinks
    on disk so Obsidian's graph view + backlinks pane render."""
    path = tmp_path / "entry.md"
    entry = MemoryEntry(
        id="abc123",
        headline="h",
        source_refs=["sessions/conv-1_s1.md", "memory/episodic/xyz789"],
        related=["memory/entities/person/marcelo"],
        body="body",
    )
    save_entry(entry, path)
    text = path.read_text(encoding="utf-8")
    assert "[[sessions/conv-1_s1.md]]" in text
    assert "[[memory/episodic/xyz789]]" in text
    assert "[[memory/entities/person/marcelo]]" in text


def test_load_entry_strips_wikilinks_to_plain_refs(tmp_path: Path) -> None:
    """In-memory representation stays as plain strings; the wikilink
    wrap lives at the disk boundary only."""
    path = tmp_path / "entry.md"
    path.write_text(
        "---\n"
        "id: abc\n"
        "headline: h\n"
        "source_refs:\n"
        "  - '[[sessions/conv-1_s1.md]]'\n"
        "  - '[[memory/episodic/xyz]]'\n"
        "related:\n"
        "  - '[[memory/entities/person/marcelo]]'\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    entry = load_entry(path)
    assert entry.source_refs == [
        "sessions/conv-1_s1.md", "memory/episodic/xyz",
    ]
    assert entry.related == ["memory/entities/person/marcelo"]


def test_load_entry_tolerant_of_plain_refs_backward_compat(
    tmp_path: Path,
) -> None:
    """Workspaces written before P9 Cambio 3 have plain refs. Loader
    must accept both formats so we don't need a migration step."""
    path = tmp_path / "entry.md"
    path.write_text(
        "---\n"
        "id: abc\n"
        "headline: h\n"
        "source_refs:\n"
        "  - sessions/conv-1_s1.md\n"
        "  - memory/episodic/xyz\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    entry = load_entry(path)
    assert entry.source_refs == [
        "sessions/conv-1_s1.md", "memory/episodic/xyz",
    ]


def test_load_entry_tolerant_of_mixed_format(tmp_path: Path) -> None:
    """Hand-edited entry with one ref wrapped, one plain — both work."""
    path = tmp_path / "entry.md"
    path.write_text(
        "---\n"
        "id: abc\n"
        "headline: h\n"
        "source_refs:\n"
        "  - '[[wrapped/ref]]'\n"
        "  - plain/ref\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    entry = load_entry(path)
    assert entry.source_refs == ["wrapped/ref", "plain/ref"]


def test_round_trip_preserves_refs_with_wikilink_serialization(
    tmp_path: Path,
) -> None:
    """write → read returns identical in-memory entry (modulo
    wikilink-strip on load)."""
    path = tmp_path / "entry.md"
    original = MemoryEntry(
        id="abc",
        headline="h",
        source_refs=["sessions/conv-1_s1.md"],
        related=["memory/episodic/xyz"],
        body="body",
    )
    save_entry(original, path)
    loaded = load_entry(path)
    assert loaded.source_refs == original.source_refs
    assert loaded.related == original.related


def test_save_entry_idempotent_on_already_wikilinked_refs(
    tmp_path: Path,
) -> None:
    """If in-memory entry already has wikilink-formatted refs (e.g.
    user passed them that way), don't double-wrap."""
    path = tmp_path / "entry.md"
    entry = MemoryEntry(
        id="abc",
        headline="h",
        source_refs=["[[already/wrapped]]"],
        body="body",
    )
    save_entry(entry, path)
    text = path.read_text(encoding="utf-8")
    # Must NOT see [[[[already/wrapped]]]] (double wrap)
    assert "[[[[" not in text
    assert "[[already/wrapped]]" in text
