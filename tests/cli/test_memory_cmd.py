"""Tests for `durin memory` CLI subcommand."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.memory_cmd import memory_app
from durin.memory.dream import ConsolidationResult, DreamConsolidator, EntryRef


runner = CliRunner()


@pytest.fixture
def populated_workspace(tmp_path: Path) -> Path:
    """Workspace with one consolidated entity page + git history.

    Returns workspace root. Patches load_config so the CLI sees this
    workspace.
    """
    # Build a real page + history via DreamConsolidator with a stub LLM.
    consolidator = DreamConsolidator(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm("person:marcelo", rev=1),
    )
    result = consolidator.consolidate_entity(
        "person:marcelo",
        [EntryRef(id="e1", timestamp="2026-04-10", text="initial observation")],
    )
    consolidator.apply("person:marcelo", result)

    # Second consolidation — rev 2
    consolidator._llm_invoke = _make_stub_llm("person:marcelo", rev=2)
    result2 = consolidator.consolidate_entity(
        "person:marcelo",
        [EntryRef(id="e2", timestamp="2026-04-15", text="follow-up observation")],
    )
    consolidator.apply("person:marcelo", result2)
    return tmp_path


def _make_stub_llm(entity_ref: str, *, rev: int):
    type_, slug = entity_ref.split(":", 1)
    response = (
        "===PAGE===\n"
        "---\n"
        f"type: {type_}\n"
        "name: Marcelo Marmol\n"
        "aliases: [Marcelo, marcelo]\n"
        f"dream_processed_through: {rev * 10}\n"
        "---\n"
        "\n"
        "# Marcelo Marmol\n"
        "\n"
        f"## Current State (rev {rev})\n"
        f"Observation set {rev}.\n"
        "===COMMIT===\n"
        f"Consolidate {entity_ref} (rev {rev})\n"
        "\n"
        f"Consolidation pass {rev} merging recent observations.\n"
        "\n"
        f"Sources: e{rev}\n"
        f"Entities-touched: {entity_ref}\n"
        "Entities-referenced: project:durin\n"
        f"Cursor-after: {rev * 10}\n"
        "===END===\n"
    )

    def stub(prompt: str, *, model: str) -> str:
        return response

    return stub


def _patch_workspace(workspace: Path):
    """Patch _workspace_root() so the CLI sees `workspace` instead of ~/.durin/."""
    return patch("durin.cli.memory_cmd._workspace_root", return_value=workspace)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_history_lists_commits(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["history", "person:marcelo"])
    assert result.exit_code == 0, result.output
    assert "person:marcelo" in result.output
    assert "rev 1" in result.output
    assert "rev 2" in result.output


def test_history_for_unknown_entity_shows_empty(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["history", "person:nobody"])
    assert result.exit_code == 0
    assert "No history" in result.output


def test_history_rejects_bad_entity_format(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["history", "no-colon"])
    assert result.exit_code != 0
    assert "type" in result.output.lower() or "slug" in result.output.lower()


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_head_outputs_current_page(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["show", "person:marcelo"])
    assert result.exit_code == 0
    # rev 2 is HEAD, so we should see rev 2 body
    assert "rev 2" in result.output
    assert "Marcelo" in result.output


def _commit_sha_for(workspace: Path, subject_contains: str) -> str:
    """Helper: get commit SHA directly from git, not from CLI output."""
    from durin.utils.git_repo import GitRepo

    repo = GitRepo(workspace / "memory")
    for c in repo.log():
        if subject_contains in c.subject:
            return c.sha
    raise RuntimeError(f"no commit matching {subject_contains!r}")


def test_show_at_specific_revision(populated_workspace: Path) -> None:
    """Show the page at the first revision."""
    old_sha = _commit_sha_for(populated_workspace, "rev 1")
    with _patch_workspace(populated_workspace):
        result = runner.invoke(
            memory_app, ["show", "person:marcelo", "--rev", old_sha],
        )
    assert result.exit_code == 0, result.output
    # Should show rev 1 content, not rev 2
    assert "rev 1" in result.output
    assert "rev 2" not in result.output


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_diff_rejects_bad_revs_format(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["diff", "person:marcelo", "not-a-range"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------


def test_expand_shows_page_metadata_and_history(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["expand", "person:marcelo"])
    assert result.exit_code == 0, result.output
    assert "Marcelo Marmol" in result.output
    assert "History" in result.output
    assert "Sources" in result.output
    assert "Related entities" in result.output
    assert "project:durin" in result.output  # from Entities-referenced trailer


def test_expand_missing_page_fails(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["expand", "person:nobody"])
    assert result.exit_code != 0
    assert "No page" in result.output


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------


def test_revert_with_yes_prints_guidance(populated_workspace: Path) -> None:
    """Revert is partially-implemented; v1 prints guidance to use git directly."""
    sha = _commit_sha_for(populated_workspace, "rev 2")
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["revert", sha, "--yes"])
    assert result.exit_code == 0
    # rich may wrap "git revert" across lines; assert both words present.
    assert "git" in result.output
    assert "revert" in result.output


# ---------------------------------------------------------------------------
# uninitialized repo handling
# ---------------------------------------------------------------------------


def test_history_on_uninitialized_repo(tmp_path: Path) -> None:
    """When memory/.git/ doesn't exist, history reports clean error."""
    with _patch_workspace(tmp_path):
        result = runner.invoke(memory_app, ["history", "person:marcelo"])
    assert result.exit_code != 0
    assert "not been initialized" in result.output
