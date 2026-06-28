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
    CLASS_INDEX_FILENAME,
    VAULT_README_FILENAME,
    ensure_class_indices,
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
        "souls/", "workflows/", "workflows-runs/", "cron/", "work/",
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

    monkeypatch.setattr(vr, "atomic_write_text", _explode)
    result = ensure_vault_readme(tmp_path)
    assert result is False
    # The file should not have been created.
    assert not (tmp_path / VAULT_README_FILENAME).exists()


# ---------------------------------------------------------------------------
# P9 Cambio 5: per-class `_INDEX.md` navigation helpers
# ---------------------------------------------------------------------------


def _make_class_dirs(workspace: Path) -> None:
    """Create empty class folders so `ensure_class_indices` has
    something to write into."""
    from durin.memory.paths import MEMORY_CLASSES
    for cls in MEMORY_CLASSES:
        (workspace / "memory" / cls).mkdir(parents=True, exist_ok=True)


def test_class_indices_written_once_per_class(tmp_path: Path) -> None:
    """First call writes one _INDEX.md per existing class folder."""
    _make_class_dirs(tmp_path)
    n = ensure_class_indices(tmp_path)
    from durin.memory.paths import MEMORY_CLASSES
    assert n == len(MEMORY_CLASSES)
    for cls in MEMORY_CLASSES:
        target = tmp_path / "memory" / cls / CLASS_INDEX_FILENAME
        assert target.is_file(), f"missing _INDEX.md in {cls}"


def test_class_indices_idempotent(tmp_path: Path) -> None:
    """Second call writes nothing (existing files preserved)."""
    _make_class_dirs(tmp_path)
    ensure_class_indices(tmp_path)
    # Mutate one index — second call must not overwrite
    target = tmp_path / "memory" / "episodic" / CLASS_INDEX_FILENAME
    custom = "# my edits\nnothing here\n"
    target.write_text(custom, encoding="utf-8")
    n_again = ensure_class_indices(tmp_path)
    assert n_again == 0
    assert target.read_text(encoding="utf-8") == custom


def test_class_indices_skipped_when_class_dir_missing(tmp_path: Path) -> None:
    """If memory/<class>/ doesn't exist, no index written for it."""
    # Create only episodic
    (tmp_path / "memory" / "episodic").mkdir(parents=True)
    n = ensure_class_indices(tmp_path)
    assert n == 1
    assert (tmp_path / "memory" / "episodic" / CLASS_INDEX_FILENAME).is_file()
    assert not (tmp_path / "memory" / "stable" / CLASS_INDEX_FILENAME).exists()


def test_class_indices_not_indexed_by_walk_memory(tmp_path: Path) -> None:
    """The `_` prefix in CLASS_INDEX_FILENAME ensures `walk_memory()`
    skips them — the navigation helpers must NOT pollute FTS / vector
    index as if they were memory entries."""
    from durin.memory.paths import walk_memory

    _make_class_dirs(tmp_path)
    # Also drop a real entry so walk_memory has something to yield
    (tmp_path / "memory" / "episodic" / "real_entry.md").write_text(
        "x", encoding="utf-8",
    )
    ensure_class_indices(tmp_path)
    paths = list(walk_memory(tmp_path))
    names = [p.name for p in paths]
    assert "real_entry.md" in names
    # No _INDEX.md must appear
    assert CLASS_INDEX_FILENAME not in names
    assert not any(n.startswith("_") for n in names)


def test_class_indices_content_mentions_dataview(tmp_path: Path) -> None:
    """Indices should include a Dataview query snippet so users with
    the plugin can browse immediately."""
    _make_class_dirs(tmp_path)
    ensure_class_indices(tmp_path)
    episodic_index = (
        tmp_path / "memory" / "episodic" / CLASS_INDEX_FILENAME
    ).read_text(encoding="utf-8")
    assert "dataview" in episodic_index.lower()


def test_walk_memory_skips_underscore_files(tmp_path: Path) -> None:
    """Defensive regression: any `_*.md` in memory/ subfolders must be
    excluded. P9 Cambio 5 establishes this convention."""
    from durin.memory.paths import walk_memory
    (tmp_path / "memory" / "stable").mkdir(parents=True)
    (tmp_path / "memory" / "stable" / "_helper.md").write_text("x", encoding="utf-8")
    (tmp_path / "memory" / "stable" / "real.md").write_text("x", encoding="utf-8")
    names = [p.name for p in walk_memory(tmp_path)]
    assert "real.md" in names
    assert "_helper.md" not in names


def test_walk_memory_skips_underscore_folders(tmp_path: Path) -> None:
    """Defensive: an entire `_*` folder under memory/ should also be
    skipped, in case future tooling drops navigation/state there."""
    from durin.memory.paths import walk_memory
    (tmp_path / "memory" / "episodic").mkdir(parents=True)
    (tmp_path / "memory" / "episodic" / "real.md").write_text("x", encoding="utf-8")
    (tmp_path / "memory" / "_internal").mkdir(parents=True)
    (tmp_path / "memory" / "_internal" / "stuff.md").write_text("x", encoding="utf-8")
    names = [str(p.relative_to(tmp_path / "memory")) for p in walk_memory(tmp_path)]
    assert "episodic/real.md" in names
    assert not any(n.startswith("_internal") for n in names)
