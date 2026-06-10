"""Tests for the execute_code tool (programmatic tool calling)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from durin.agent.tools.code_execution import (
    CodeExecutionConfig,
    ExecuteCodeTool,
)
from durin.agent.tools.filesystem import ReadFileTool

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="execute_code is UDS-only in v1",
)


def _tool(tmp_path: Path, **cfg) -> ExecuteCodeTool:
    config = CodeExecutionConfig(**cfg)
    return ExecuteCodeTool(
        tools={"read_file": ReadFileTool(workspace=tmp_path)},
        config=config,
        workspace=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_stdout_returned(tmp_path):
    tool = _tool(tmp_path)
    raw = await tool.execute(code="print('hello-from-script')")
    result = json.loads(raw)
    assert result["status"] == "success"
    assert "hello-from-script" in result["output"]
    assert result["tool_calls_made"] == 0


@pytest.mark.asyncio
async def test_script_calls_read_file_via_rpc(tmp_path):
    (tmp_path / "data.txt").write_text("needle-in-file\n", encoding="utf-8")
    tool = _tool(tmp_path)
    code = (
        "from durin_tools import read_file\n"
        "content = read_file('data.txt', limit=10)\n"
        "print('FOUND' if 'needle-in-file' in content else 'MISSING')\n"
    )
    result = json.loads(await tool.execute(code=code))
    assert result["status"] == "success"
    assert "FOUND" in result["output"]
    assert result["tool_calls_made"] == 1
    # The raw file content is NOT in the result — only what the script printed.
    assert "needle-in-file" not in result["output"]


@pytest.mark.asyncio
async def test_disallowed_tool_rejected(tmp_path):
    tool = _tool(tmp_path)
    code = (
        "from durin_tools import _call\n"
        "try:\n"
        "    _call('exec', {'command': 'id'})\n"
        "    print('ALLOWED')\n"
        "except RuntimeError as e:\n"
        "    print('REJECTED:', e)\n"
    )
    result = json.loads(await tool.execute(code=code))
    assert "REJECTED" in result["output"]
    assert "not available" in result["output"]


@pytest.mark.asyncio
async def test_tool_call_cap(tmp_path):
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    tool = _tool(tmp_path, max_tool_calls=2)
    code = (
        "from durin_tools import read_file\n"
        "ok = 0\n"
        "err = None\n"
        "for _ in range(3):\n"
        "    try:\n"
        "        read_file('x.txt')\n"
        "        ok += 1\n"
        "    except RuntimeError as e:\n"
        "        err = str(e)\n"
        "print('ok=', ok, 'err=', err)\n"
    )
    result = json.loads(await tool.execute(code=code))
    assert "ok= 2" in result["output"]
    assert "limit reached" in result["output"]


@pytest.mark.asyncio
async def test_timeout_kills_script(tmp_path):
    tool = _tool(tmp_path, timeout_s=2)
    result = json.loads(await tool.execute(code="import time; time.sleep(30)"))
    assert result["status"] == "timeout"
    assert "timed out" in result["error"]


@pytest.mark.asyncio
async def test_stdout_truncation(tmp_path):
    tool = _tool(tmp_path, max_stdout_bytes=2000)
    code = "print('HEAD-MARK'); print('x' * 10000); print('TAIL-MARK')"
    result = json.loads(await tool.execute(code=code))
    assert "OUTPUT TRUNCATED" in result["output"]
    assert "HEAD-MARK" in result["output"]
    assert "TAIL-MARK" in result["output"]


@pytest.mark.asyncio
async def test_env_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SECRET_KEY", "super-secret-value")
    tool = _tool(tmp_path)
    code = "import os; print(os.environ.get('MY_SECRET_KEY', 'ABSENT'))"
    result = json.loads(await tool.execute(code=code))
    assert "ABSENT" in result["output"]
    assert "super-secret-value" not in result["output"]


@pytest.mark.asyncio
async def test_script_error_reported(tmp_path):
    tool = _tool(tmp_path)
    result = json.loads(await tool.execute(code="raise ValueError('boom-42')"))
    assert result["status"] == "error"
    assert "boom-42" in result["error"]


def test_windows_disabled():
    class Ctx:
        class config:
            code_execution = CodeExecutionConfig()

    if sys.platform == "win32":
        assert ExecuteCodeTool.enabled(Ctx) is False
