"""`durin memory forget` CLI tests (P12 Phase 0).

Closes the gap between VAULT_README's promise ("If you want to delete:
use ``durin memory forget <uri>``") and the missing implementation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.memory_cmd import memory_app


runner = CliRunner()


def _seed_entry(workspace: Path, class_name: str, entry_id: str) -> Path:
    p = workspace / "memory" / class_name / f"{entry_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {entry_id}\nheadline: {entry_id} hl\n---\n\nbody\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def workspace_config(tmp_path: Path):
    """Patch the loader so the CLI's ``_workspace_root()`` hits tmp_path
    and memory is disabled (so the vector-index cleanup branch is a no-op
    without needing fastembed)."""
    fake_cfg = SimpleNamespace(
        workspace_path=tmp_path,
        memory=SimpleNamespace(
            enabled=False,
            embedding=SimpleNamespace(model=""),
        ),
    )
    with patch("durin.cli.memory_cmd.load_config", return_value=fake_cfg):
        yield tmp_path


def test_forget_episodic_archives_file(workspace_config: Path) -> None:
    src = _seed_entry(workspace_config, "episodic", "obs-1")
    result = runner.invoke(
        memory_app, ["forget", "memory/episodic/obs-1", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert not src.exists(), "source must be moved"
    archived = (
        workspace_config / "memory" / "archive" / "episodic" / "obs-1.md"
    )
    assert archived.exists(), "archive copy must exist"
    body = archived.read_text(encoding="utf-8")
    assert "archived_at:" in body
    assert "archived_reason: user_forget" in body


@pytest.mark.parametrize(
    "klass", ["stable", "corpus", "session_summary"],
)
def test_forget_generic_classes(workspace_config: Path, klass: str) -> None:
    src = _seed_entry(workspace_config, klass, "entry-x")
    result = runner.invoke(
        memory_app, ["forget", f"memory/{klass}/entry-x", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert not src.exists()
    archived = workspace_config / "memory" / "archive" / klass / "entry-x.md"
    assert archived.exists()


def test_forget_refuses_entities(workspace_config: Path) -> None:
    """Entity pages have their own absorb/revert lifecycle."""
    entity = workspace_config / "memory" / "entities" / "person" / "marcelo.md"
    entity.parent.mkdir(parents=True, exist_ok=True)
    entity.write_text("---\ntype: person\n---\nbody\n", encoding="utf-8")
    result = runner.invoke(
        memory_app, ["forget", "memory/entities/person/marcelo", "--yes"],
    )
    # `entities/person/marcelo` has 4 path parts; the URI parser already
    # rejects it (it expects `memory/<class>/<id>` = 3 parts).
    assert result.exit_code != 0
    assert entity.exists(), "rejected path must NOT be moved"


def test_forget_refuses_entities_explicit_three_part_uri(
    workspace_config: Path,
) -> None:
    """A 3-part URI where class is literally 'entities' (no nested
    type) is still refused with a helpful message."""
    result = runner.invoke(
        memory_app, ["forget", "memory/entities/marcelo", "--yes"],
    )
    assert result.exit_code == 2
    assert "entity" in result.output.lower()


def test_forget_missing_entry_exits_1(workspace_config: Path) -> None:
    result = runner.invoke(
        memory_app, ["forget", "memory/episodic/ghost", "--yes"],
    )
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_forget_bad_uri_format(workspace_config: Path) -> None:
    result = runner.invoke(memory_app, ["forget", "not-a-uri", "--yes"])
    assert result.exit_code != 0
    assert "memory/<class>/<id>" in result.output


def test_forget_strips_dot_md_suffix(workspace_config: Path) -> None:
    """Operators often paste URIs with the `.md` suffix from a file
    listing; the parser tolerates it."""
    _seed_entry(workspace_config, "episodic", "obs-2")
    result = runner.invoke(
        memory_app, ["forget", "memory/episodic/obs-2.md", "--yes"],
    )
    assert result.exit_code == 0, result.output


def test_forget_unsupported_class(workspace_config: Path) -> None:
    """Classes outside the forgettable set are rejected before any IO."""
    result = runner.invoke(
        memory_app, ["forget", "memory/garbage/x", "--yes"],
    )
    assert result.exit_code == 2
    assert "unsupported" in result.output.lower()


def test_forget_without_yes_prompts_and_aborts(
    workspace_config: Path,
) -> None:
    """Without --yes, an empty stdin (CliRunner default) means the
    confirm() returns False → exit 1, file untouched."""
    src = _seed_entry(workspace_config, "stable", "fact-1")
    result = runner.invoke(memory_app, ["forget", "memory/stable/fact-1"])
    assert result.exit_code == 1
    assert src.exists(), "without confirmation the file must stay"
