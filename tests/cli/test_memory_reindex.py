"""`durin memory reindex` CLI smoke tests (doc 02 §7.1)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.memory_cmd import memory_app
from durin.memory.entity_page import EntityPage
from durin.memory.fts_index import FTSIndex

runner = CliRunner()


def _seed_workspace(workspace: Path) -> None:
    page = EntityPage(
        type="person", name="Marcelo", aliases=["m"],
        body="Architect of durin",
    )
    page.save(workspace / "memory" / "entities" / "person" / "marcelo.md")


@pytest.fixture
def workspace_config(tmp_path: Path):
    """Patch the loader so the CLI's `_workspace_root()` hits tmp_path."""
    from types import SimpleNamespace
    fake_cfg = SimpleNamespace(workspace_path=tmp_path)
    with patch("durin.cli.memory_cmd.load_config", return_value=fake_cfg):
        yield tmp_path


def test_reindex_fts_only_smoke(workspace_config: Path) -> None:
    _seed_workspace(workspace_config)
    result = runner.invoke(memory_app, ["reindex", "--target", "fts"])
    assert result.exit_code == 0, result.output
    assert "Indexed:" in result.output
    with FTSIndex.open(workspace_config) as idx:
        assert any(h.uri == "person:marcelo" for h in idx.search("Marcelo"))


def test_reindex_unknown_target_rejected(workspace_config: Path) -> None:
    _seed_workspace(workspace_config)
    result = runner.invoke(memory_app, ["reindex", "--target", "bogus"])
    assert result.exit_code != 0
    assert "must be one of" in result.output


def test_reindex_empty_workspace_exits_clean(tmp_path: Path) -> None:
    """No memory/ → friendly message, exit 0."""
    from types import SimpleNamespace
    fake_cfg = SimpleNamespace(workspace_path=tmp_path)
    with patch("durin.cli.memory_cmd.load_config", return_value=fake_cfg):
        result = runner.invoke(memory_app, ["reindex", "--target", "fts"])
    assert result.exit_code == 0
    assert "nothing to index" in result.output.lower()
