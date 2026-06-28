"""Tests for the post-edit check helper and its write/edit wiring."""

from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path

import pytest

from durin.agent.tools.post_edit_check import (
    PostEditCheckConfig,
    run_post_edit_check,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="fake checker scripts are POSIX shell",
)


def _make_checker(tmp_path: Path, body: str) -> Path:
    """Create an executable fake checker script."""
    script = tmp_path / "fake_checker.sh"
    script.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _config(tmp_path: Path, body: str, **overrides) -> PostEditCheckConfig:
    script = _make_checker(tmp_path, body)
    defaults = {"checkers": {"py": f"{script} {{file}}"}}
    defaults.update(overrides)
    return PostEditCheckConfig(**defaults)


class TestRunPostEditCheck:

    @pytest.mark.asyncio
    async def test_no_checker_for_extension(self, tmp_path):
        cfg = _config(tmp_path, "exit 1")
        target = tmp_path / "notes.md"
        target.write_text("x", encoding="utf-8")
        assert await run_post_edit_check(target, cfg) is None

    @pytest.mark.asyncio
    async def test_none_config_skips(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x", encoding="utf-8")
        assert await run_post_edit_check(target, None) is None

    @pytest.mark.asyncio
    async def test_disabled_config_skips(self, tmp_path):
        cfg = _config(tmp_path, "echo issue; exit 1", enable=False)
        target = tmp_path / "a.py"
        target.write_text("x", encoding="utf-8")
        assert await run_post_edit_check(target, cfg) is None

    @pytest.mark.asyncio
    async def test_missing_binary_skips(self, tmp_path):
        cfg = PostEditCheckConfig(
            checkers={"py": "definitely-not-a-binary-xyz {file}"},
        )
        target = tmp_path / "a.py"
        target.write_text("x", encoding="utf-8")
        assert await run_post_edit_check(target, cfg) is None

    @pytest.mark.asyncio
    async def test_clean_exit_returns_none(self, tmp_path):
        cfg = _config(tmp_path, "exit 0")
        target = tmp_path / "a.py"
        target.write_text("x", encoding="utf-8")
        assert await run_post_edit_check(target, cfg) is None

    @pytest.mark.asyncio
    async def test_issues_reported(self, tmp_path):
        cfg = _config(tmp_path, 'echo "$1:1:1: F401 unused import"; exit 1')
        target = tmp_path / "a.py"
        target.write_text("import os\n", encoding="utf-8")
        result = await run_post_edit_check(target, cfg)
        assert result is not None
        assert "post-edit check" in result
        assert "F401 unused import" in result

    @pytest.mark.asyncio
    async def test_line_cap(self, tmp_path):
        cfg = _config(
            tmp_path, "for i in $(seq 1 50); do echo issue-$i; done; exit 1",
            max_lines=5,
        )
        target = tmp_path / "a.py"
        target.write_text("x", encoding="utf-8")
        result = await run_post_edit_check(target, cfg)
        assert result is not None
        assert "issue-5" in result
        assert "issue-6" not in result
        assert "and 45 more" in result

    @pytest.mark.asyncio
    async def test_timeout_skips(self, tmp_path):
        cfg = _config(tmp_path, "sleep 5; exit 1", timeout_s=1)
        target = tmp_path / "a.py"
        target.write_text("x", encoding="utf-8")
        assert await run_post_edit_check(target, cfg) is None

    def test_default_checkers_include_ruff(self):
        cfg = PostEditCheckConfig()
        assert "py" in cfg.checkers
        assert "ruff" in cfg.checkers["py"]

    def test_default_checkers_include_json(self):
        cfg = PostEditCheckConfig()
        assert "json" in cfg.checkers
        assert "json.tool" in cfg.checkers["json"]

    @pytest.mark.skipif(
        shutil.which("python3") is None, reason="python3 not on PATH",
    )
    @pytest.mark.asyncio
    async def test_default_json_checker_flags_invalid(self, tmp_path):
        cfg = PostEditCheckConfig()  # default json -> python3 -m json.tool
        target = tmp_path / "bad.json"
        target.write_text('{"a": 1,}', encoding="utf-8")
        result = await run_post_edit_check(target, cfg)
        assert result is not None
        assert "post-edit check" in result

    @pytest.mark.skipif(
        shutil.which("python3") is None, reason="python3 not on PATH",
    )
    @pytest.mark.asyncio
    async def test_default_json_checker_accepts_valid(self, tmp_path):
        cfg = PostEditCheckConfig()
        target = tmp_path / "good.json"
        target.write_text('{"a": 1}', encoding="utf-8")
        assert await run_post_edit_check(target, cfg) is None


# ---------------------------------------------------------------------------
# Integration with write_file / edit_file
# ---------------------------------------------------------------------------


class TestToolIntegration:

    @pytest.mark.asyncio
    async def test_write_file_appends_findings(self, tmp_path):
        from durin.agent.tools.filesystem import WriteFileTool
        cfg = _config(tmp_path, 'echo "fake-finding-W1"; exit 1')
        tool = WriteFileTool(workspace=tmp_path, post_edit_config=cfg)
        result = await tool.execute(path="mod.py", content="import os\n")
        assert "Successfully wrote" in result
        assert "fake-finding-W1" in result

    @pytest.mark.asyncio
    async def test_edit_file_appends_findings(self, tmp_path):
        from durin.agent.tools.filesystem import EditFileTool, ReadFileTool
        cfg = _config(tmp_path, 'echo "fake-finding-E1"; exit 1')
        f = tmp_path / "mod.py"
        f.write_text("x = 1\n", encoding="utf-8")
        await ReadFileTool(workspace=tmp_path).execute(path="mod.py")
        tool = EditFileTool(workspace=tmp_path, post_edit_config=cfg)
        result = await tool.execute(path="mod.py", old_text="x = 1", new_text="x = 2")
        assert "Successfully edited" in result
        assert "fake-finding-E1" in result

    @pytest.mark.asyncio
    async def test_clean_checker_leaves_message_unchanged(self, tmp_path):
        from durin.agent.tools.filesystem import WriteFileTool
        cfg = _config(tmp_path, "exit 0")
        tool = WriteFileTool(workspace=tmp_path, post_edit_config=cfg)
        result = await tool.execute(path="mod.py", content="x = 1\n")
        assert "Successfully wrote" in result
        assert "post-edit check" not in result

    @pytest.mark.asyncio
    async def test_no_config_default_unchanged(self, tmp_path):
        from durin.agent.tools.filesystem import WriteFileTool
        tool = WriteFileTool(workspace=tmp_path)
        result = await tool.execute(path="notes.md", content="hello")
        assert "Successfully wrote" in result
