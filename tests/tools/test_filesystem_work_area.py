"""Tests: filesystem tools resolve plain relative paths under the per-session work dir."""
import pytest

from durin.agent.tools.context import RequestContext
from durin.agent.tools.filesystem import ReadFileTool, WriteFileTool


def _ctx(session_key: str = "telegram:7") -> RequestContext:
    return RequestContext(channel="telegram", chat_id="7", session_key=session_key)


@pytest.mark.asyncio
async def test_plain_write_lands_in_work_dir(tmp_path):
    tool = WriteFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    tool.set_context(_ctx())
    await tool.execute(path="note.md", content="hello")
    assert (tmp_path / "work" / "telegram_7" / "note.md").read_text() == "hello"
    assert not (tmp_path / "note.md").exists()


@pytest.mark.asyncio
async def test_write_then_read_roundtrip(tmp_path):
    write_tool = WriteFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    read_tool = ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    ctx = _ctx()
    write_tool.set_context(ctx)
    read_tool.set_context(ctx)

    await write_tool.execute(path="a.py", content="x = 1\n")
    result = await read_tool.execute(path="a.py")
    assert "x = 1" in result


@pytest.mark.asyncio
async def test_no_session_context_uses_workspace_root(tmp_path):
    """Backward compat: no set_context call → plain relative path resolves to workspace root."""
    tool = WriteFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    # No set_context call → legacy behavior
    await tool.execute(path="note.md", content="legacy")
    assert (tmp_path / "note.md").read_text() == "legacy"
    assert not (tmp_path / "work").exists()


@pytest.mark.asyncio
async def test_managed_prefix_resolves_to_workspace_even_with_session(tmp_path):
    """Managed prefix paths (e.g. 'memory/foo.md') anchor to workspace root, not work dir."""
    managed = tmp_path / "memory"
    managed.mkdir()
    tool = WriteFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    tool.set_context(_ctx())
    await tool.execute(path="memory/note.md", content="managed")
    # File must land at workspace root / managed prefix, not in the session work dir.
    assert (tmp_path / "memory" / "note.md").read_text() == "managed"
    assert not (tmp_path / "work" / "telegram_7" / "memory" / "note.md").exists()
