"""A tool called from an ``execute_code`` script answers to the agent mode.

The runner's mode gate sees only ``execute_code``; the tools a script calls
are dispatched by the tool itself, over RPC from the script's subprocess.
Each such call gets the decision and the argument preparation a direct call
to that tool would get under the mode of the ``execute_code`` call that is
running, and the description the model reads lists only the script tools
that mode allows.

Every test goes through ``AgentRunner._execute_tools`` with a real script
subprocess, the same path a model's ``execute_code`` call takes.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.agent.agent_mode import BUILD_MODE, AgentMode, refuse_tool_call
from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.agent.tools.code_execution import CodeExecutionConfig, ExecuteCodeTool
from durin.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import AgentDefaults
from durin.providers.base import ToolCallRequest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="execute_code is UDS-only",
)

_MAX_CHARS = AgentDefaults().max_tool_result_chars
_HINT = "\n\n[Analyze the error above and try a different approach.]"

DENY_WRITE = AgentMode(
    name="no-write", description="denies the file-writing tool",
    denied=frozenset({"write_file"}),
)
ALLOWLIST_WITHOUT_WRITE = AgentMode(
    name="script-read", description="allowlist without the file-writing tool",
    allowed=frozenset({"execute_code", "read_file", "list_dir"}),
)
ALLOWLIST_WITH_WRITE = AgentMode(
    name="script-write", description="allowlist with the file-writing tool",
    allowed=frozenset({"execute_code", "read_file", "list_dir", "write_file"}),
)
SCRIPT_ONLY = AgentMode(
    name="script-only", description="allows execute_code and no tool it can call",
    allowed=frozenset({"execute_code"}),
)

_UNSET = object()


class _RecordingWrite(WriteFileTool):
    """The real write tool, recording every call that reaches ``execute``."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace=workspace)
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return await super().execute(**kwargs)


def _registry(tmp_path: Path, read_tool: ReadFileTool | None = None):
    (tmp_path / "data.txt").write_text("data-contents\n", encoding="utf-8")
    write = _RecordingWrite(tmp_path)
    read = read_tool or ReadFileTool(workspace=tmp_path)
    listing = ListDirTool(workspace=tmp_path)
    registry = ToolRegistry()
    for tool in (read, write, listing):
        registry.register(tool)
    registry.register(ExecuteCodeTool(
        tools={"read_file": read, "write_file": write, "list_dir": listing},
        config=CodeExecutionConfig(),
        workspace=str(tmp_path),
    ))
    return registry, write


def _spec(registry: ToolRegistry, mode: Any = _UNSET) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[],
        tools=registry,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_CHARS,
        mode_provider=None if mode is _UNSET else (lambda: mode),
    )


async def _call(
    registry: ToolRegistry, mode: Any, name: str, arguments: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    runner = AgentRunner(MagicMock())
    results, events, _ = await runner._execute_tools(
        _spec(registry, mode),
        [ToolCallRequest(id="c1", name=name, arguments=arguments)],
        {}, {}, set(),
    )
    return results[0], events[0]


async def _run_script(registry: ToolRegistry, mode: Any, code: str) -> dict[str, Any]:
    raw, event = await _call(registry, mode, "execute_code", {"code": code})
    assert event["status"] == "ok", raw
    return json.loads(raw)


_TRY_WRITE = (
    "from durin_tools import read_file, write_file\n"
    "try:\n"
    "    write_file('out.txt', 'written')\n"
    "    print('WROTE')\n"
    "except RuntimeError as e:\n"
    "    print('REFUSED:', e)\n"
    "print('AFTER', read_file('data.txt').strip())\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [DENY_WRITE, ALLOWLIST_WITHOUT_WRITE])
async def test_script_call_to_a_refused_tool_gets_the_refusal(tmp_path, mode):
    registry, write = _registry(tmp_path)

    direct, direct_event = await _call(
        registry, mode, "write_file", {"path": "direct.txt", "content": "x"},
    )
    result = await _run_script(registry, mode, _TRY_WRITE)

    refusal = refuse_tool_call(mode, "write_file")
    assert direct_event["detail"] == f"denied by mode '{mode.name}'"
    assert direct == refusal + _HINT
    assert write.calls == [], "a refused tool's execute must never run"
    assert not (tmp_path / "out.txt").exists()
    assert result["status"] == "success", result
    assert f"REFUSED: {refusal}" in result["output"]
    # The rest of the script ran.
    assert "AFTER data-contents" in result["output"]


@pytest.mark.asyncio
async def test_uncaught_refusal_fails_the_script_with_the_refusal_text(tmp_path):
    registry, write = _registry(tmp_path)

    result = await _run_script(
        registry, DENY_WRITE,
        "from durin_tools import write_file\nwrite_file('out.txt', 'written')\n",
    )

    assert write.calls == []
    assert result["status"] == "error"
    assert refuse_tool_call(DENY_WRITE, "write_file") in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [ALLOWLIST_WITH_WRITE, BUILD_MODE, _UNSET],
    ids=["allowlist", "build", "no-mode"],
)
async def test_script_call_to_an_allowed_tool_runs_with_cast_arguments(tmp_path, mode):
    registry, write = _registry(tmp_path)

    result = await _run_script(registry, mode, (
        _TRY_WRITE
        + "from durin_tools import _call\n"
        + "print('LISTED', _call('list_dir', {'path': '.', 'max_entries': '1'}))\n"
    ))

    assert result["status"] == "success", result
    assert "WROTE" in result["output"]
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "written"
    assert len(write.calls) == 1
    # The string max_entries was cast to the schema's integer: one entry listed.
    listed = result["output"].split("LISTED ", 1)[1]
    assert listed.count("📄") == 1, listed


@pytest.mark.asyncio
async def test_invalid_script_argument_gives_the_direct_call_error(tmp_path):
    registry, _ = _registry(tmp_path)
    bad = {"path": ".", "max_entries": "many"}

    direct, direct_event = await _call(registry, ALLOWLIST_WITH_WRITE, "list_dir", bad)
    result = await _run_script(registry, ALLOWLIST_WITH_WRITE, (
        "from durin_tools import _call\n"
        "try:\n"
        f"    _call('list_dir', {bad!r})\n"
        "except RuntimeError as e:\n"
        "    print('ERR=' + str(e))\n"
    ))

    assert direct_event["status"] == "error"
    script_error = result["output"].split("ERR=", 1)[1].rstrip("\n")
    assert script_error.startswith("Error: Invalid parameters for tool 'list_dir'")
    assert direct == script_error + _HINT


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [BUILD_MODE, _UNSET], ids=["build", "no-mode"])
async def test_no_restricting_mode_keeps_the_result_format(tmp_path, mode):
    registry, _ = _registry(tmp_path)

    result = await _run_script(registry, mode, _TRY_WRITE)

    assert set(result) == {"status", "output", "tool_calls_made", "duration_seconds"}
    assert result["status"] == "success"
    assert result["tool_calls_made"] == 2
    assert result["output"] == "WROTE\nAFTER data-contents\n"


@pytest.mark.asyncio
async def test_unknown_script_tool_lists_only_what_the_mode_allows(tmp_path):
    registry, _ = _registry(tmp_path)
    code = (
        "from durin_tools import _call\n"
        "try:\n"
        "    _call('exec', {'command': 'id'})\n"
        "except RuntimeError as e:\n"
        "    print(e)\n"
    )

    restricted = await _run_script(registry, DENY_WRITE, code)
    unrestricted = await _run_script(registry, BUILD_MODE, code)

    assert "(allowed: list_dir, read_file)" in restricted["output"]
    assert "(allowed: list_dir, read_file, write_file)" in unrestricted["output"]


@pytest.mark.asyncio
async def test_concurrent_scripts_under_different_modes_keep_their_own_mode(tmp_path):
    """Two ``execute_code`` calls on one shared tool instance, each under its
    own mode, overlap inside their dispatches: each script's write answers
    to its own call's mode."""
    arrived = 0
    both_in = asyncio.Event()

    class _Rendezvous(ReadFileTool):
        """Holds each script's first read until both scripts are inside a
        dispatch, so the two calls are in flight at the same time."""

        async def execute(self, **kwargs: Any) -> Any:
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_in.set()
            await both_in.wait()
            return await super().execute(**kwargs)

    registry, write = _registry(tmp_path, _Rendezvous(workspace=tmp_path))

    def script(tag: str) -> str:
        return (
            "from durin_tools import read_file, write_file\n"
            "read_file('data.txt')\n"
            "try:\n"
            f"    write_file('{tag}.txt', '{tag}')\n"
            f"    print('{tag} WROTE')\n"
            "except RuntimeError as e:\n"
            f"    print('{tag} REFUSED')\n"
        )

    async with asyncio.timeout(120):
        refused, allowed = await asyncio.gather(
            _run_script(registry, DENY_WRITE, script("denied")),
            _run_script(registry, BUILD_MODE, script("allowed")),
        )

    assert both_in.is_set()
    assert refused["output"] == "denied REFUSED\n"
    assert allowed["output"] == "allowed WROTE\n"
    assert [c["path"] for c in write.calls] == ["allowed.txt"]
    assert not (tmp_path / "denied.txt").exists()


def _execute_code_description(registry: ToolRegistry, mode: Any) -> str:
    for schema in AgentRunner._active_tool_definitions(_spec(registry, mode)):
        if schema["function"]["name"] == "execute_code":
            return schema["function"]["description"]
    raise AssertionError("execute_code not offered")


@pytest.mark.parametrize("restricted_first", [True, False])
def test_description_lists_only_script_tools_the_mode_allows(tmp_path, restricted_first):
    """One registry serves every session and caches tool schemas: what a
    restricted turn is told must not leak into an unrestricted one, or the
    other way round, whichever renders first."""
    registry, _ = _registry(tmp_path)
    order = [DENY_WRITE, ALLOWLIST_WITHOUT_WRITE, SCRIPT_ONLY, BUILD_MODE]
    if not restricted_first:
        order.reverse()

    described = {mode.name: _execute_code_description(registry, mode) for mode in order}

    for name in (DENY_WRITE.name, ALLOWLIST_WITHOUT_WRITE.name):
        assert "write_file" not in described[name]
        assert "read_file" in described[name]
        assert "list_dir" in described[name]
    for tool in ("write_file", "read_file", "list_dir"):
        assert tool not in described[SCRIPT_ONLY.name]
        assert tool in described[BUILD_MODE.name]


def test_every_generated_stub_passes_its_tools_argument_preparation(tmp_path):
    """Each ``durin_tools`` function, called with only its required
    arguments, sends arguments that the real tool's ``prepare_call``
    accepts — the preparation every script call now goes through."""
    from durin.agent.tools.code_execution import _ALLOWED_TOOLS, _generate_stub
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.agent.tools.search import GrepTool
    from durin.agent.tools.web import WebFetchTool, WebSearchTool

    sent: list[tuple[str, dict[str, Any]]] = []
    stub: dict[str, Any] = {}
    exec(compile(_generate_stub(), "durin_tools.py", "exec"), stub)
    stub["_call"] = lambda tool, args: sent.append((tool, args))

    stub["read_file"]("a.txt")
    stub["write_file"]("a.txt", "text")
    stub["edit_file"]("a.txt", "old", "new")
    stub["grep"]("needle")
    stub["list_dir"](".")
    stub["web_search"]("query")
    stub["web_fetch"]("https://example.invalid/")
    stub["memory_search"]("query")

    tool = ExecuteCodeTool(tools={t.name: t for t in (
        ReadFileTool(workspace=tmp_path), WriteFileTool(workspace=tmp_path),
        EditFileTool(workspace=tmp_path), GrepTool(workspace=tmp_path),
        ListDirTool(workspace=tmp_path), WebSearchTool(), WebFetchTool(),
        MemorySearchTool(workspace=tmp_path),
    )})
    assert sorted(name for name, _ in sent) == sorted(_ALLOWED_TOOLS)
    for name, args in sent:
        _, _, error = tool._registry.prepare_call(name, args)
        assert error is None, (name, args, error)
