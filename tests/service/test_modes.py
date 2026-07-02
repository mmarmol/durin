"""Tests for ModesService — lists registered agent modes for the picker."""

import pytest

from durin.service.modes import ModesListQuery, ModesService, ToolsListQuery
from durin.service.principal import Principal


class _FakeTool:
    def __init__(self, name: str, description: str, read_only: bool) -> None:
        self.name = name
        self.description = description
        self.read_only = read_only


class _FakeRegistry:
    """Minimal stand-in for the live loop's ToolRegistry (name lookup only)."""

    def __init__(self, *tools: _FakeTool) -> None:
        self._t = {t.name: t for t in tools}

    @property
    def tool_names(self) -> list[str]:
        return list(self._t)

    def get(self, name: str):
        return self._t.get(name)


@pytest.mark.asyncio
async def test_list_exposes_builtins_with_flag_and_description():
    result = await ModesService().list(ModesListQuery(), Principal.local())
    by_name = {m["name"]: m for m in result.modes}
    # The three built-ins are always registered.
    assert {"build", "plan", "explore"} <= set(by_name)
    for name in ("build", "plan", "explore"):
        assert by_name[name]["builtin"] is True
        assert by_name[name]["description"]  # human-readable, non-empty
        assert "icon" in by_name[name]  # present in the DTO, may be None
    # build is full access and ships no icon → the picker falls back to a glyph.
    assert by_name["build"]["icon"] is None


@pytest.mark.asyncio
async def test_tools_catalog_projects_live_registry():
    reg = _FakeRegistry(
        _FakeTool("read_file", "Read a file.", True),
        _FakeTool("edit_file", "Edit a file.", False),
        _FakeTool("mcp_srv_do", "An MCP tool.", True),
    )
    svc = ModesService(tool_registry_resolver=lambda: reg)
    result = await svc.tools(ToolsListQuery(), Principal.local())
    by_name = {t["name"]: t for t in result.tools}
    assert by_name["read_file"] == {
        "name": "read_file",
        "description": "Read a file.",
        "read_only": True,
        "source": "builtin",
    }
    assert by_name["edit_file"]["read_only"] is False
    assert by_name["mcp_srv_do"]["source"] == "mcp"
    # Built-ins sort ahead of MCP tools (the primary curation surface first).
    names = [t["name"] for t in result.tools]
    assert names.index("edit_file") < names.index("mcp_srv_do")


@pytest.mark.asyncio
async def test_tools_catalog_falls_back_to_loader_without_live_loop():
    # No resolver → loader discovery. The editor must never be empty: the core
    # read-only built-ins have to surface with their read_only flag intact.
    result = await ModesService().tools(ToolsListQuery(), Principal.local())
    by_name = {t["name"]: t for t in result.tools}
    assert "read_file" in by_name
    assert by_name["read_file"]["read_only"] is True
    assert by_name["read_file"]["source"] == "builtin"
