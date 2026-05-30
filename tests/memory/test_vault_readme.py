"""Tests for the workspace VAULT_README.md generator.

P9 (2026-05-30): the README is the one-line handoff that explains the
on-disk layout to humans browsing the workspace (Obsidian users, webui
MemoryGraphView, anyone with file-tree access). These tests pin the
idempotency contract and the placement (workspace root, not inside
`memory/` — where it would be indexed as a memory entry).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.vault_readme import (
    VAULT_README_FILENAME,
    ensure_vault_readme,
)


def test_writes_readme_when_missing(tmp_path: Path) -> None:
    """First boot: README doesn't exist → write it, return True."""
    result = ensure_vault_readme(tmp_path)
    assert result is True
    readme = tmp_path / VAULT_README_FILENAME
    assert readme.is_file()
    text = readme.read_text(encoding="utf-8")
    assert "# durin workspace" in text
    # The README must call out the read-only contract explicitly.
    assert "Read this, don't edit it" in text or "read this" in text.lower()


def test_idempotent_when_already_exists(tmp_path: Path) -> None:
    """Second boot: README already there → no-op, return False, content
    preserved (user edits survive)."""
    readme = tmp_path / VAULT_README_FILENAME
    custom = "# my custom notes\nnothing to see here\n"
    readme.write_text(custom, encoding="utf-8")
    result = ensure_vault_readme(tmp_path)
    assert result is False
    assert readme.read_text(encoding="utf-8") == custom


def test_writes_at_workspace_root_not_in_memory(tmp_path: Path) -> None:
    """README must NOT live inside `memory/` — `walk_memory()` would
    pick it up and index it as a memory entry, which is wrong."""
    ensure_vault_readme(tmp_path)
    assert (tmp_path / VAULT_README_FILENAME).is_file()
    # Should not appear inside memory/.
    assert not (tmp_path / "memory" / VAULT_README_FILENAME).exists()


def test_readme_mentions_key_folders(tmp_path: Path) -> None:
    """The README is the navigational map of the workspace. It must
    name the folders a human will see."""
    ensure_vault_readme(tmp_path)
    text = (tmp_path / VAULT_README_FILENAME).read_text(encoding="utf-8")
    for folder in (
        "memory/", "stable/", "episodic/", "corpus/",
        "session_summary/", "entities/", "archive/",
        "ingested/", ".durin/",
    ):
        assert folder in text, f"VAULT_README missing folder reference: {folder}"


def test_readme_mentions_recommended_viewers(tmp_path: Path) -> None:
    """The README is the on-ramp to read-only browsing. Mention the
    intended viewers so a new user knows where to start."""
    ensure_vault_readme(tmp_path)
    text = (tmp_path / VAULT_README_FILENAME).read_text(encoding="utf-8")
    assert "Obsidian" in text
    assert "MemoryGraphView" in text or "webui" in text.lower()


def test_graceful_failure_on_unwritable_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If write fails (e.g. read-only mount) the function returns False
    and logs a warning. The agent boot must NOT crash because a help
    file couldn't be written."""
    import durin.memory.vault_readme as vr

    def _explode(*_args, **_kwargs):
        raise PermissionError("read-only fs")

    monkeypatch.setattr(Path, "write_text", _explode)
    result = ensure_vault_readme(tmp_path)
    assert result is False
    # The file should not have been created.
    assert not (tmp_path / VAULT_README_FILENAME).exists()
