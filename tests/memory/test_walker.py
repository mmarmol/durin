"""Tests for the shared workspace walker `walk_memory`.

Per `docs/memory/02_indexing.md` §6.5 + `01_data_and_entities.md` §3.6:
the walker is the single chokepoint for "what .md files under memory/
should be processed". Excludes archive/ (consolidated content) and
pending/ (intake buffer) by default. An `include_archive=True` opt-in
lets recovery surfaces walk archive explicitly.

These tests lock the chokepoint contract. Every caller in the codebase
(indexer, entity_ranker, alias bootstrap, etc.) MUST use this walker;
the tests verify the walker's own behavior.
"""

from __future__ import annotations

from pathlib import Path

from durin.memory.paths import walk_class, walk_memory


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\nid: x\nheadline: x\n---\n\nbody\n", encoding="utf-8")


def test_walk_memory_yields_entity_pages(tmp_path: Path) -> None:
    """Entity pages under memory/entities/<type>/*.md are yielded."""
    _touch(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    _touch(tmp_path / "memory" / "entities" / "project" / "durin.md")

    paths = sorted(walk_memory(tmp_path))

    assert paths == [
        tmp_path / "memory" / "entities" / "person" / "marcelo.md",
        tmp_path / "memory" / "entities" / "project" / "durin.md",
    ]


def test_walk_memory_yields_episodic_stable_corpus(tmp_path: Path) -> None:
    """Episodic, stable, and corpus class folders are yielded."""
    _touch(tmp_path / "memory" / "episodic" / "2026-05-23-x.md")
    _touch(tmp_path / "memory" / "stable" / "marcelo-prefs.md")
    _touch(tmp_path / "memory" / "corpus" / "paper-chunk-3.md")

    paths = sorted(walk_memory(tmp_path))

    assert paths == [
        tmp_path / "memory" / "corpus" / "paper-chunk-3.md",
        tmp_path / "memory" / "episodic" / "2026-05-23-x.md",
        tmp_path / "memory" / "stable" / "marcelo-prefs.md",
    ]


def test_walk_memory_excludes_archive_by_default(tmp_path: Path) -> None:
    """memory/archive/** is invisible to default callers (§3.6 of doc 01)."""
    _touch(tmp_path / "memory" / "episodic" / "kept.md")
    _touch(tmp_path / "memory" / "archive" / "episodic" / "consolidated.md")
    _touch(tmp_path / "memory" / "archive" / "entities" / "person" / "absorbed.md")

    paths = sorted(walk_memory(tmp_path))

    assert paths == [tmp_path / "memory" / "episodic" / "kept.md"]


def test_walk_memory_includes_archive_when_requested(tmp_path: Path) -> None:
    """Explicit opt-in surfaces archived files for recovery / diagnostic use."""
    _touch(tmp_path / "memory" / "episodic" / "kept.md")
    _touch(tmp_path / "memory" / "archive" / "episodic" / "consolidated.md")

    paths = sorted(walk_memory(tmp_path, include_archive=True))

    assert paths == [
        tmp_path / "memory" / "archive" / "episodic" / "consolidated.md",
        tmp_path / "memory" / "episodic" / "kept.md",
    ]


def test_walk_memory_excludes_pending(tmp_path: Path) -> None:
    """memory/pending/** is intake buffer; never yielded (even with archive flag)."""
    _touch(tmp_path / "memory" / "episodic" / "real.md")
    _touch(tmp_path / "memory" / "pending" / "draft.md")

    paths_default = sorted(walk_memory(tmp_path))
    paths_with_archive = sorted(walk_memory(tmp_path, include_archive=True))

    assert paths_default == [tmp_path / "memory" / "episodic" / "real.md"]
    assert paths_with_archive == [tmp_path / "memory" / "episodic" / "real.md"]


def test_walk_memory_empty_workspace(tmp_path: Path) -> None:
    """Workspace without memory/ yields nothing — does not raise."""
    paths = list(walk_memory(tmp_path))
    assert paths == []


def test_walk_memory_ignores_non_md_files(tmp_path: Path) -> None:
    """Only .md files are yielded (json metadata, jsonl, etc. ignored)."""
    _touch(tmp_path / "memory" / "episodic" / "entry.md")
    (tmp_path / "memory" / "episodic" / "entry.meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "memory" / "episodic" / "notes.txt").write_text("scratch", encoding="utf-8")

    paths = sorted(walk_memory(tmp_path))

    assert paths == [tmp_path / "memory" / "episodic" / "entry.md"]


def test_walk_class_episodic(tmp_path: Path) -> None:
    """walk_class('episodic') yields only memory/episodic/*.md."""
    _touch(tmp_path / "memory" / "episodic" / "a.md")
    _touch(tmp_path / "memory" / "episodic" / "b.md")
    _touch(tmp_path / "memory" / "stable" / "ignored.md")
    _touch(tmp_path / "memory" / "entities" / "person" / "marcelo.md")

    paths = sorted(walk_class(tmp_path, "episodic"))

    assert paths == [
        tmp_path / "memory" / "episodic" / "a.md",
        tmp_path / "memory" / "episodic" / "b.md",
    ]


def test_walk_class_entities_recurses_into_types(tmp_path: Path) -> None:
    """walk_class('entities') recurses into <type>/ subdirectories."""
    _touch(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    _touch(tmp_path / "memory" / "entities" / "person" / "susana.md")
    _touch(tmp_path / "memory" / "entities" / "project" / "durin.md")
    _touch(tmp_path / "memory" / "episodic" / "ignored.md")

    paths = sorted(walk_class(tmp_path, "entities"))

    assert paths == [
        tmp_path / "memory" / "entities" / "person" / "marcelo.md",
        tmp_path / "memory" / "entities" / "person" / "susana.md",
        tmp_path / "memory" / "entities" / "project" / "durin.md",
    ]


def test_walk_class_archive_excluded_by_default(tmp_path: Path) -> None:
    """walk_class('episodic') does NOT cross into archive/episodic/ by default."""
    _touch(tmp_path / "memory" / "episodic" / "live.md")
    _touch(tmp_path / "memory" / "archive" / "episodic" / "consolidated.md")

    paths = sorted(walk_class(tmp_path, "episodic"))

    assert paths == [tmp_path / "memory" / "episodic" / "live.md"]


def test_walk_class_archive_via_class_name(tmp_path: Path) -> None:
    """walk_class('archive') yields archived files recursively (explicit recovery surface)."""
    _touch(tmp_path / "memory" / "archive" / "episodic" / "consolidated.md")
    _touch(tmp_path / "memory" / "archive" / "entities" / "person" / "absorbed.md")
    _touch(tmp_path / "memory" / "episodic" / "ignored.md")

    paths = sorted(walk_class(tmp_path, "archive"))

    assert paths == [
        tmp_path / "memory" / "archive" / "entities" / "person" / "absorbed.md",
        tmp_path / "memory" / "archive" / "episodic" / "consolidated.md",
    ]


def test_walk_class_empty_dir(tmp_path: Path) -> None:
    """Missing class dir yields nothing — does not raise."""
    paths = list(walk_class(tmp_path, "episodic"))
    assert paths == []


def test_walk_class_unknown_class_raises(tmp_path: Path) -> None:
    """Unknown class name raises ValueError (avoid silent typo bugs)."""
    import pytest

    with pytest.raises(ValueError, match="unknown memory class"):
        list(walk_class(tmp_path, "ephemeral"))
