"""Tests for `durin uninstall` enumeration + deletion logic."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.commands import app
from durin.cli.uninstall import (
    _format_bytes,
    _path_size,
    collect_targets,
    default_target_groups,
    run_uninstall,
)
from durin.cli.upgrade import PYPI_DIST_NAME

runner = CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override `Path.home()` so the uninstall walks our fixtures, not the real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("durin.cli.uninstall._home", lambda: home)
    # The durin data root resolves via DURIN_HOME; point it at the fake tree
    # (the kit cache still derives from _home()).
    monkeypatch.setenv("DURIN_HOME", str(home / ".durin"))

    # Build a realistic state tree under the fake home.
    (home / ".durin").mkdir()
    (home / ".durin" / "config.json").write_text('{"x":1}', encoding="utf-8")
    (home / ".durin" / "config.json.bak").write_text("{}", encoding="utf-8")
    (home / ".durin" / "workspace").mkdir()
    (home / ".durin" / "workspace" / "scratch.md").write_text("hi", encoding="utf-8")
    (home / ".durin" / "sessions").mkdir()
    (home / ".durin" / "history").mkdir()
    (home / ".durin" / "media").mkdir()
    (home / ".cache" / "durin").mkdir(parents=True)
    (home / ".cache" / "durin" / "telemetry").mkdir()
    (home / ".cache" / "durin" / "telemetry" / "log.jsonl").write_text("{}\n", encoding="utf-8")
    (home / ".cache" / "durin" / "models").mkdir()
    (home / ".cache" / "durin" / "archive").mkdir()
    return home


def test_default_target_groups_lists_expected_paths() -> None:
    groups = default_target_groups()
    names = {g.name for g in groups}
    assert {"Config", "Workspace", "Cache", "Other state"}.issubset(names)


def test_default_target_groups_includes_workspace_when_passed(tmp_path: Path) -> None:
    ws = tmp_path / "myproj"
    groups = default_target_groups(workspace=ws)
    assert any("Per-workspace" in g.name for g in groups)


def test_collect_targets_skips_missing_paths(fake_home: Path) -> None:
    # Cron dir does not exist in the fixture; it should not appear in targets.
    targets = collect_targets(
        keep_config=False, keep_workspace=False, keep_cache=False, workspace=None
    )
    str_paths = [str(p) for _g, p, _s in targets]
    assert not any(p.endswith(".durin/cron") for p in str_paths)


def test_collect_targets_includes_existing_paths(fake_home: Path) -> None:
    targets = collect_targets(
        keep_config=False, keep_workspace=False, keep_cache=False, workspace=None
    )
    str_paths = {str(p) for _g, p, _s in targets}
    assert str(fake_home / ".durin" / "config.json") in str_paths
    assert str(fake_home / ".durin" / "workspace") in str_paths
    assert str(fake_home / ".cache" / "durin" / "telemetry") in str_paths


def test_collect_targets_honors_keep_config(fake_home: Path) -> None:
    targets = collect_targets(
        keep_config=True, keep_workspace=False, keep_cache=False, workspace=None
    )
    str_paths = {str(p) for _g, p, _s in targets}
    assert str(fake_home / ".durin" / "config.json") not in str_paths
    # Workspace still slated for removal.
    assert str(fake_home / ".durin" / "workspace") in str_paths


def test_collect_targets_honors_keep_workspace(fake_home: Path) -> None:
    targets = collect_targets(
        keep_config=False, keep_workspace=True, keep_cache=False, workspace=None
    )
    str_paths = {str(p) for _g, p, _s in targets}
    assert str(fake_home / ".durin" / "workspace") not in str_paths


def test_collect_targets_honors_keep_cache(fake_home: Path) -> None:
    targets = collect_targets(
        keep_config=False, keep_workspace=False, keep_cache=True, workspace=None
    )
    str_paths = {str(p) for _g, p, _s in targets}
    assert str(fake_home / ".cache" / "durin" / "telemetry") not in str_paths


def test_run_uninstall_yes_actually_deletes(fake_home: Path) -> None:
    rc = run_uninstall(
        assume_yes=True,
        purge=False,
        keep_config=False,
        keep_workspace=False,
        keep_cache=False,
        workspace=None,
    )
    assert rc == 0
    assert not (fake_home / ".durin" / "config.json").exists()
    assert not (fake_home / ".durin" / "workspace").exists()
    assert not (fake_home / ".cache" / "durin" / "telemetry").exists()


def test_run_uninstall_keep_config_preserves_file(fake_home: Path) -> None:
    rc = run_uninstall(
        assume_yes=True,
        purge=False,
        keep_config=True,
        keep_workspace=False,
        keep_cache=False,
        workspace=None,
    )
    assert rc == 0
    assert (fake_home / ".durin" / "config.json").exists()
    assert not (fake_home / ".durin" / "workspace").exists()


def test_run_uninstall_purge_spawns_pip(fake_home: Path) -> None:
    with patch("durin.cli.uninstall.subprocess.Popen") as mock_popen:
        rc = run_uninstall(
            assume_yes=True,
            purge=True,
            keep_config=False,
            keep_workspace=False,
            keep_cache=False,
            workspace=None,
        )
    assert rc == 0
    mock_popen.assert_called_once()
    cmd = mock_popen.call_args.args[0]
    # Must target the real PyPI distribution name (`durin-agent`), not the
    # bare import/CLI name `durin`: `durin` isn't an installed distribution,
    # so `pip uninstall -y durin` is a silent no-op and --purge leaves the
    # package on disk.
    assert cmd[:5] == [sys.executable, "-m", "pip", "uninstall", "-y"]
    assert cmd[-1] == PYPI_DIST_NAME
    assert PYPI_DIST_NAME == "durin-agent"
    assert "durin" not in cmd


def test_run_uninstall_aborts_when_prompt_declined(fake_home: Path) -> None:
    with patch("typer.confirm", return_value=False):
        rc = run_uninstall(
            assume_yes=False,
            purge=False,
            keep_config=False,
            keep_workspace=False,
            keep_cache=False,
            workspace=None,
        )
    assert rc == 1
    # State untouched.
    assert (fake_home / ".durin" / "config.json").exists()


def test_cli_uninstall_dry_run_prints_plan(fake_home: Path) -> None:
    """Without --yes, the runner sees the prompt and (no input) declines."""
    result = runner.invoke(app, ["uninstall"], input="n\n")
    # Aborted by the user → exit 1 + state untouched.
    assert result.exit_code == 1
    assert (fake_home / ".durin" / "config.json").exists()
    assert "Path" in result.output  # rendered as part of the plan table


def test_cli_uninstall_yes_deletes(fake_home: Path) -> None:
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert not (fake_home / ".durin" / "config.json").exists()


def test_format_bytes() -> None:
    assert _format_bytes(0) == "0 B"
    assert _format_bytes(1023) == "1023 B"
    assert _format_bytes(2048).startswith("2.0 KB")
    assert _format_bytes(5 * 1024 * 1024).startswith("5.0 MB")


def test_path_size_returns_zero_for_missing(tmp_path: Path) -> None:
    assert _path_size(tmp_path / "nope") == 0


def test_path_size_counts_directory_contents(tmp_path: Path) -> None:
    d = tmp_path / "x"
    d.mkdir()
    (d / "a.txt").write_text("hello", encoding="utf-8")
    (d / "b.txt").write_text("world!", encoding="utf-8")
    assert _path_size(d) == 11
