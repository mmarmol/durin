"""Tests for memory directory layout helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.paths import (
    MEMORY_CLASSES,
    dream_dir,
    ingested_dir,
    ingested_entry_dir,
    memory_class_dir,
    memory_dir,
)


def test_memory_classes_are_canonical() -> None:
    # A10 (2026-05-28): added `session_summary` so the walker picks
    # up `memory/session_summary/*.md` (the markdown projection of
    # the consolidator's session summaries).
    assert set(MEMORY_CLASSES) == {
        "stable", "episodic", "corpus", "pending", "session_summary",
    }


def test_memory_dir_creates_directory(tmp_path: Path) -> None:
    result = memory_dir(tmp_path)
    assert result == tmp_path / "memory"
    assert result.is_dir()


def test_memory_dir_idempotent(tmp_path: Path) -> None:
    first = memory_dir(tmp_path)
    second = memory_dir(tmp_path)
    assert first == second
    assert first.is_dir()


@pytest.mark.parametrize("class_name", ["stable", "episodic", "corpus", "pending"])
def test_memory_class_dir_each_class(tmp_path: Path, class_name: str) -> None:
    result = memory_class_dir(tmp_path, class_name)
    assert result == tmp_path / "memory" / class_name
    assert result.is_dir()


def test_memory_class_dir_unknown_class_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown memory class"):
        memory_class_dir(tmp_path, "feedback")


def test_ingested_dir_creates_directory(tmp_path: Path) -> None:
    result = ingested_dir(tmp_path)
    assert result == tmp_path / "ingested"
    assert result.is_dir()


def test_ingested_entry_dir_creates_directory(tmp_path: Path) -> None:
    result = ingested_entry_dir(tmp_path, "doc-007")
    assert result == tmp_path / "ingested" / "doc-007"
    assert result.is_dir()


def test_dream_dir_creates_directory(tmp_path: Path) -> None:
    result = dream_dir(tmp_path)
    assert result == tmp_path / "dream"
    assert result.is_dir()
