"""Archive consumed episodic entries after a successful Dream apply.

Per `docs/architecture/memory/05_dream_cold_path.md` §7: for each unique
`provenance` ref in the applied patch that points under
`memory/episodic/`, **move** the file to
`memory/archive/episodic/<id>.md` and (best-effort) drop the
corresponding LanceDB row.

Stable / corpus / pending refs are NOT archived (§5.3-§5.5 doc 01).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from durin.memory.dream_archive_consumed import (
    ArchiveConsumedResult,
    archive_consumed_episodic,
)
from durin.memory.dream_patch_parser import ParsedDreamOutput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_episodic(workspace: Path, name: str, body: str = "x") -> Path:
    path = workspace / "memory" / "episodic" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {name}\nheadline: head\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _write_corpus(workspace: Path, name: str) -> Path:
    path = workspace / "memory" / "corpus" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {name}\nheadline: head\n---\n\nchunk\n",
        encoding="utf-8",
    )
    return path


class _FakeVectorIndex:
    def __init__(self) -> None:
        self.deletes: list[str] = []

    def delete_by_id(self, entry_id: str) -> None:
        self.deletes.append(entry_id)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_archives_referenced_episodic(tmp_path: Path) -> None:
    _write_episodic(tmp_path, "2026-05-23-foo")
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1,
             "provenance": "episodic/2026-05-23-foo.md"},
        ],
        body_delta="",
        commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path,
        entity_ref="person:marcelo",
        parsed=output,
    )
    assert isinstance(result, ArchiveConsumedResult)
    assert result.archived == ["episodic/2026-05-23-foo.md"]
    assert result.errors == []

    src = tmp_path / "memory" / "episodic" / "2026-05-23-foo.md"
    dst = tmp_path / "memory" / "archive" / "episodic" / "2026-05-23-foo.md"
    assert not src.exists()
    assert dst.exists()
    archived_text = dst.read_text(encoding="utf-8")
    assert "archived_at:" in archived_text
    assert "archived_into: person:marcelo" in archived_text


def test_dedups_repeated_provenance(tmp_path: Path) -> None:
    """If 3 patch ops cite the same source, archive runs once."""
    _write_episodic(tmp_path, "shared")
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/a", "value": 1,
             "provenance": "episodic/shared.md"},
            {"op": "add", "path": "/attributes/b", "value": 2,
             "provenance": "episodic/shared.md"},
            {"op": "add", "path": "/aliases/-", "value": "z",
             "provenance": "episodic/shared.md"},
        ],
        body_delta="", commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path, entity_ref="person:m", parsed=output,
    )
    assert result.archived == ["episodic/shared.md"]


def test_multiple_distinct_refs_all_archived(tmp_path: Path) -> None:
    _write_episodic(tmp_path, "a")
    _write_episodic(tmp_path, "b")
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1,
             "provenance": "episodic/a.md"},
            {"op": "add", "path": "/attributes/y", "value": 2,
             "provenance": "episodic/b.md"},
        ],
        body_delta="", commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path, entity_ref="person:m", parsed=output,
    )
    assert sorted(result.archived) == [
        "episodic/a.md", "episodic/b.md",
    ]


# ---------------------------------------------------------------------------
# Non-episodic refs are NOT archived
# ---------------------------------------------------------------------------


def test_stable_provenance_skipped(tmp_path: Path) -> None:
    """Stable entries are never auto-archived (§5.4 doc 01)."""
    p = tmp_path / "memory" / "stable" / "decision.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\nid: d\nheadline: h\n---\n", encoding="utf-8")
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1,
             "provenance": "stable/decision.md"},
        ],
        body_delta="", commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path, entity_ref="person:m", parsed=output,
    )
    assert result.archived == []
    assert p.exists()  # untouched


def test_archive_tolerates_date_prefix_provenance(tmp_path: Path) -> None:
    """LLM-emitted provenance like `2023-03-26/e3caeb1ee93a` must still
    archive — defensive parser. Bug 2026-05-31: the consolidator render
    used `[<timestamp> / <id>]` brackets, inducing the LLM to cite that
    same shape instead of `episodic/<id>.md`. The render is now fixed
    (test_prompt_entry_header_uses_episodic_path_format), but old
    workspaces + model variance mean the archive parser must remain
    tolerant.
    """
    _write_episodic(tmp_path, "e3caeb1ee93a")
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1,
             "provenance": "2023-03-26/e3caeb1ee93a"},
        ],
        body_delta="", commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path, entity_ref="person:m", parsed=output,
    )
    assert result.archived, (
        f"expected episodic to archive, got: archived={result.archived}, "
        f"errors={result.errors}"
    )
    assert not (tmp_path / "memory" / "episodic" / "e3caeb1ee93a.md").exists()
    assert (
        tmp_path / "memory" / "archive" / "episodic" / "e3caeb1ee93a.md"
    ).exists()


def test_archive_tolerates_bare_id_provenance(tmp_path: Path) -> None:
    """Bare ids (no path prefix, no date) — what we see in `Sources:`
    commit trailers. Same defensive contract as the date-prefix case.
    """
    _write_episodic(tmp_path, "abc123def456")
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1,
             "provenance": "abc123def456"},
        ],
        body_delta="", commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path, entity_ref="person:m", parsed=output,
    )
    assert result.archived, (
        f"expected episodic to archive, got: archived={result.archived}, "
        f"errors={result.errors}"
    )


def test_corpus_provenance_skipped(tmp_path: Path) -> None:
    p = _write_corpus(tmp_path, "chunk-3")
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1,
             "provenance": "corpus/chunk-3.md"},
        ],
        body_delta="", commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path, entity_ref="person:m", parsed=output,
    )
    assert result.archived == []
    assert p.exists()


def test_empty_patch_yields_empty_result(tmp_path: Path) -> None:
    output = ParsedDreamOutput(
        patch_ops=[], body_delta="", commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path, entity_ref="person:m", parsed=output,
    )
    assert result.archived == []
    assert result.errors == []


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_missing_file_records_error_and_continues(tmp_path: Path) -> None:
    _write_episodic(tmp_path, "exists")
    # `gone.md` doesn't exist on disk.
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1,
             "provenance": "episodic/gone.md"},
            {"op": "add", "path": "/attributes/y", "value": 2,
             "provenance": "episodic/exists.md"},
        ],
        body_delta="", commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path, entity_ref="person:m", parsed=output,
    )
    # The good one still got archived.
    assert result.archived == ["episodic/exists.md"]
    # The missing one is recorded but does not abort the run.
    assert len(result.errors) == 1
    assert "episodic/gone.md" in result.errors[0]


# ---------------------------------------------------------------------------
# Vector index deletion (best-effort)
# ---------------------------------------------------------------------------


def test_vector_index_delete_by_uri(tmp_path: Path) -> None:
    """When a vector index is supplied, deleted episodic refs are
    passed to `delete_by_id` so the next search doesn't surface them.
    The id is the bare entry id, NOT the path."""
    _write_episodic(tmp_path, "to-archive")
    idx = _FakeVectorIndex()
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1,
             "provenance": "episodic/to-archive.md"},
        ],
        body_delta="", commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path, entity_ref="person:m", parsed=output,
        vector_index=idx,
    )
    assert result.archived == ["episodic/to-archive.md"]
    assert idx.deletes == ["to-archive"]


def test_vector_index_none_is_fine(tmp_path: Path) -> None:
    """No vector index supplied → archive still runs, no error."""
    _write_episodic(tmp_path, "x")
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1,
             "provenance": "episodic/x.md"},
        ],
        body_delta="", commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path, entity_ref="person:m", parsed=output,
        vector_index=None,
    )
    assert result.archived == ["episodic/x.md"]


def test_vector_index_delete_error_logged_not_raised(tmp_path: Path) -> None:
    """Vector delete failure must not abort the archive step."""
    _write_episodic(tmp_path, "x")

    class BrokenIndex:
        def delete_by_id(self, entry_id: str) -> None:
            raise RuntimeError("lancedb crashed")

    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1,
             "provenance": "episodic/x.md"},
        ],
        body_delta="", commit_message="s",
    )
    result = archive_consumed_episodic(
        workspace=tmp_path, entity_ref="person:m", parsed=output,
        vector_index=BrokenIndex(),
    )
    # File still archived; the vector failure is logged as a non-fatal
    # error string.
    assert result.archived == ["episodic/x.md"]
    assert any("lancedb crashed" in e for e in result.errors)
