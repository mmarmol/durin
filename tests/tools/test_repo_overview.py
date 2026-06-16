"""Tests for RepoOverviewTool (Sprint A / T1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from durin.agent.tools import file_state
from durin.agent.tools.repo_overview import (
    _STRUCTURE_LIMIT,
    RepoOverviewTool,
)
from durin.telemetry.logger import (
    TelemetryLogger,
    bind_telemetry,
    reset_telemetry,
)


@pytest.fixture(autouse=True)
def _clear_file_state():
    file_state.clear()
    yield
    file_state.clear()


@pytest.fixture
def telemetry_log(tmp_path: Path) -> tuple[TelemetryLogger, Path]:
    log_path = tmp_path / "telemetry.jsonl"
    return TelemetryLogger(log_path), log_path


def _read_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Basic behavior
# ---------------------------------------------------------------------------


class TestRepoOverviewBasic:

    def test_empty_directory(self, tmp_path: Path):
        tool = RepoOverviewTool(workspace=tmp_path)
        result = asyncio.run(tool.execute(path="."))
        assert "Repository overview" in result
        assert "## Structure" in result

    def test_python_ecosystem_detected(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hi')\n")

        tool = RepoOverviewTool(workspace=tmp_path)
        result = asyncio.run(tool.execute(path="."))
        assert "Python" in result
        assert "pyproject.toml" in result
        assert "main.py" in result

    def test_node_ecosystem_with_pnpm(self, tmp_path: Path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "pnpm-lock.yaml").write_text("")

        tool = RepoOverviewTool(workspace=tmp_path)
        result = asyncio.run(tool.execute(path="."))
        assert "Node.js" in result
        assert "Package manager: pnpm" in result

    def test_multi_ecosystem_detected(self, tmp_path: Path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "go.mod").write_text("module x\n")
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")

        tool = RepoOverviewTool(workspace=tmp_path)
        result = asyncio.run(tool.execute(path="."))
        assert "Node.js" in result
        assert "Go" in result
        assert "Rust" in result


# ---------------------------------------------------------------------------
# Tree behavior
# ---------------------------------------------------------------------------


class TestRepoOverviewTree:

    def test_ignores_noise_dirs(self, tmp_path: Path):
        # noise dirs that should be skipped
        for name in (".git", "node_modules", "__pycache__", ".venv", "dist"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "junk").write_text("x")
        (tmp_path / "real.py").write_text("y\n")

        tool = RepoOverviewTool(workspace=tmp_path)
        result = asyncio.run(tool.execute(path="."))
        for noise in (".git", "node_modules", "__pycache__", ".venv", "dist"):
            assert noise not in result, f"noise dir leaked: {noise}"
        assert "real.py" in result

    def test_directories_sorted_before_files(self, tmp_path: Path):
        (tmp_path / "z_file.py").write_text("x\n")
        (tmp_path / "a_dir").mkdir()
        (tmp_path / "a_dir" / "inner.py").write_text("y\n")

        tool = RepoOverviewTool(workspace=tmp_path)
        result = asyncio.run(tool.execute(path="."))
        a_idx = result.index("a_dir/")
        z_idx = result.index("z_file.py")
        assert a_idx < z_idx, "directories should come before files"

    def test_depth_limit_respected(self, tmp_path: Path):
        # Build nested: root/a/b/c/d/e/f.py
        deep = tmp_path
        for n in ("a", "b", "c", "d", "e"):
            deep = deep / n
            deep.mkdir()
        (deep / "f.py").write_text("x\n")

        tool = RepoOverviewTool(workspace=tmp_path)
        result_d1 = asyncio.run(tool.execute(path=".", depth=1))
        result_d3 = asyncio.run(tool.execute(path=".", depth=3))
        # Depth 1: only "a/" visible, b/ should not be
        assert "a/" in result_d1
        assert "b/" not in result_d1
        # Depth 3: a, b, c visible — d should not be (level 3 stops descent)
        assert "c/" in result_d3

    def test_structure_limit_truncates(self, tmp_path: Path):
        # Create more than _STRUCTURE_LIMIT entries
        for i in range(_STRUCTURE_LIMIT + 50):
            (tmp_path / f"file_{i:04d}.txt").write_text("x")

        tool = RepoOverviewTool(workspace=tmp_path)
        result = asyncio.run(tool.execute(path="."))
        assert "truncated" in result.lower()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestRepoOverviewErrors:

    def test_nonexistent_path_uses_suggestion(self, tmp_path: Path):
        (tmp_path / "real_dir").mkdir()
        tool = RepoOverviewTool(workspace=tmp_path)
        result = asyncio.run(tool.execute(path="rea_dir"))
        assert "File not found" in result

    def test_path_to_file_not_dir(self, tmp_path: Path):
        (tmp_path / "file.txt").write_text("x\n")
        tool = RepoOverviewTool(workspace=tmp_path)
        result = asyncio.run(tool.execute(path="file.txt"))
        assert "Not a directory" in result


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


class TestRepoOverviewTelemetry:

    def test_emits_tool_repo_overview_event(self, tmp_path: Path, telemetry_log):
        logger, log_path = telemetry_log
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x\n")

        tool = RepoOverviewTool(workspace=tmp_path)
        token = bind_telemetry(logger)
        try:
            asyncio.run(tool.execute(path=".", depth=2))
        finally:
            reset_telemetry(token)

        events = _read_events(log_path)
        assert len(events) == 1
        evt = events[0]
        assert evt["type"] == "tool.repo_overview"
        data = evt["data"]
        assert data["depth"] == 2
        assert "Python" in data["ecosystems"]
        assert data["truncated"] is False
        assert data["structure_lines"] > 0
