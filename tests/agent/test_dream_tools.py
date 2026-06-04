from durin.config.schema import Config
from durin.agent.tools.loader import ToolLoader
from durin.agent.tools.context import ToolContext
from durin.agent.tools.registry import ToolRegistry


def test_tool_loader_scope_memory_only_returns_memory_tools():
    loader = ToolLoader()
    registry = ToolRegistry()
    ctx = ToolContext(config=Config().tools, workspace="/tmp")
    loader.load(ctx, registry, scope="memory")

    names = set(registry.tool_names)
    assert "read_file" in names
    assert "edit_file" in names
    assert "write_file" in names
    assert "list_dir" not in names
    assert "exec" not in names
    assert "message" not in names


def test_dream_registers_skill_write_not_write_file(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from durin.agent.memory import Dream, MemoryStore

    store = MemoryStore(tmp_path)
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock()
    dream = Dream(store=store, provider=provider, model="test-model")
    tools = dream._build_tools()
    names = set(tools.tool_names)
    assert "skill_write" in names
    assert "write_file" not in names
    # memory_forget is a foreground-only deletion tool; the Dream agent
    # mutates the vault through its own structured tools, never forget.
    assert "memory_forget" not in names
