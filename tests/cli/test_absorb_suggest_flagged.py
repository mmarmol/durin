"""`durin memory absorb-suggest` — flagged section (Task 6)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from durin.cli.memory_cmd import memory_app
from durin.memory.refine_dream import add_flagged

runner = CliRunner()


def _fake_config(workspace: Path):
    return SimpleNamespace(workspace_path=workspace)


def test_absorb_suggest_shows_flagged_section(tmp_path: Path) -> None:
    """When the flagged store has records, absorb-suggest prints a review section
    with the reasoning text and the absorb hint."""
    add_flagged(tmp_path, "company:a", "company:b",
                verdict="unclear", confidence=70, reasoning="the agent could not confirm")

    with patch("durin.cli.memory_cmd.load_config", return_value=_fake_config(tmp_path)), \
         patch("durin.memory.absorption.EntityAbsorption.find_candidates", return_value=[]):
        result = runner.invoke(memory_app, ["absorb-suggest"])

    assert result.exit_code == 0, result.output
    assert "Flagged by the agent" in result.output
    assert "the agent could not confirm" in result.output
    assert "company:a" in result.output
    assert "company:b" in result.output
    assert "durin memory absorb" in result.output


def test_absorb_suggest_no_flagged_section_when_empty(tmp_path: Path) -> None:
    """When the flagged store is empty, the flagged section is not printed."""
    with patch("durin.cli.memory_cmd.load_config", return_value=_fake_config(tmp_path)), \
         patch("durin.memory.absorption.EntityAbsorption.find_candidates", return_value=[]):
        result = runner.invoke(memory_app, ["absorb-suggest"])

    assert result.exit_code == 0, result.output
    assert "Flagged by the agent" not in result.output
