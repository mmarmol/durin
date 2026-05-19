"""Tests for the ``interpret_image`` bridge tool."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.tools.context import AuxProviderHandle, ToolContext
from durin.agent.tools.interpret_image import InterpretImageTool
from durin.agent.tools.loader import ToolLoader
from durin.agent.tools.registry import ToolRegistry
from durin.providers.base import LLMResponse


# Minimal valid PNG file (1×1 pixel) — used so detect_image_mime sees
# the magic bytes and treats the file as a real image.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _make_tool(tmp_path: Path) -> tuple[InterpretImageTool, MagicMock]:
    """Build a tool wired to a mocked provider; returns (tool, provider)."""
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LLMResponse(
        content="The image shows a single red pixel.",
        finish_reason="stop",
    ))
    aux = AuxProviderHandle(provider=provider, model="glm-5v-turbo")
    tool = InterpretImageTool(aux=aux, workspace=str(tmp_path))
    return tool, provider


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executes_against_aux_provider(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_BYTES)

    tool, provider = _make_tool(tmp_path)

    out = await tool.execute(image_path="shot.png", question="what's here?")

    assert "single red pixel" in out
    assert "[via glm-5v-turbo]" in out
    # Provider was called with the right shape.
    provider.chat.assert_awaited_once()
    call_kwargs = provider.chat.await_args.kwargs
    assert call_kwargs["model"] == "glm-5v-turbo"
    messages = call_kwargs["messages"]
    assert len(messages) == 1 and messages[0]["role"] == "user"
    # Content is a list of blocks; first is image_url with data URL.
    blocks = messages[0]["content"]
    assert any(b.get("type") == "image_url" for b in blocks)
    image_block = next(b for b in blocks if b.get("type") == "image_url")
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")
    # Text block carries the question.
    text_block = next(b for b in blocks if b.get("type") == "text")
    assert "what's here?" in text_block["text"]


@pytest.mark.asyncio
async def test_uses_default_question_when_omitted(tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(_PNG_BYTES)
    tool, provider = _make_tool(tmp_path)

    await tool.execute(image_path="pic.png")

    blocks = provider.chat.await_args.kwargs["messages"][0]["content"]
    text = next(b for b in blocks if b.get("type") == "text")["text"]
    assert "describe" in text.lower()


@pytest.mark.asyncio
async def test_accepts_absolute_path(tmp_path):
    img = tmp_path / "abs.png"
    img.write_bytes(_PNG_BYTES)
    tool, _ = _make_tool(tmp_path)

    out = await tool.execute(image_path=str(img), question="?")

    assert "single red pixel" in out


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_image_path_errors(tmp_path):
    tool, provider = _make_tool(tmp_path)
    out = await tool.execute()
    assert "Error" in out
    assert "image_path" in out
    provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_not_found_returns_error(tmp_path):
    tool, provider = _make_tool(tmp_path)
    out = await tool.execute(image_path="nope.png")
    assert "Error" in out
    assert "not found" in out.lower()
    provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_image_file_rejected(tmp_path):
    """The mime is detected by magic bytes, not extension. Renaming a
    text file to .png does not fool the validator."""
    fake = tmp_path / "fake.png"
    fake.write_text("not really an image")
    tool, provider = _make_tool(tmp_path)

    out = await tool.execute(image_path="fake.png", question="?")

    assert "Error" in out
    assert "supported image format" in out.lower()
    provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_exception_surfaces_as_tool_error(tmp_path):
    img = tmp_path / "img.png"
    img.write_bytes(_PNG_BYTES)
    tool, provider = _make_tool(tmp_path)
    provider.chat = AsyncMock(side_effect=RuntimeError("boom"))

    out = await tool.execute(image_path="img.png", question="?")

    assert "Error" in out
    assert "boom" in out
    assert "glm-5v-turbo" in out


@pytest.mark.asyncio
async def test_empty_response_returns_clear_error(tmp_path):
    img = tmp_path / "img.png"
    img.write_bytes(_PNG_BYTES)
    tool, provider = _make_tool(tmp_path)
    provider.chat = AsyncMock(return_value=LLMResponse(content="", finish_reason="stop"))

    out = await tool.execute(image_path="img.png", question="?")

    assert "Error" in out
    assert "no content" in out.lower()


# ---------------------------------------------------------------------------
# Tool gating / discovery
# ---------------------------------------------------------------------------


def test_enabled_only_when_vision_aux_present():
    """Without aux_providers.vision the loader skips the tool, so it
    never appears in the LLM's tool list."""
    ctx_without = ToolContext(
        config=MagicMock(), workspace="/tmp",
        subagent_manager=MagicMock(), sessions=MagicMock(),
    )
    assert InterpretImageTool.enabled(ctx_without) is False

    ctx_with = ToolContext(
        config=MagicMock(), workspace="/tmp",
        subagent_manager=MagicMock(), sessions=MagicMock(),
        aux_providers={"vision": AuxProviderHandle(provider=MagicMock(), model="m")},
    )
    assert InterpretImageTool.enabled(ctx_with) is True


def test_discovery_respects_gating():
    ctx_without = ToolContext(
        config=MagicMock(), workspace="/tmp",
        subagent_manager=MagicMock(), sessions=MagicMock(),
    )
    reg = ToolRegistry()
    loaded = ToolLoader().load(ctx_without, reg, scope="core")
    assert "interpret_image" not in loaded

    ctx_with = ToolContext(
        config=MagicMock(), workspace="/tmp",
        subagent_manager=MagicMock(), sessions=MagicMock(),
        aux_providers={"vision": AuxProviderHandle(provider=MagicMock(), model="glm-5v-turbo")},
    )
    reg2 = ToolRegistry()
    loaded2 = ToolLoader().load(ctx_with, reg2, scope="core")
    assert "interpret_image" in loaded2


def test_tool_is_in_plan_mode_allowed_set():
    """Capability bridges are read-only on the workspace — must work
    while the user is in plan mode."""
    from durin.agent.agent_mode import PLAN_MODE

    assert PLAN_MODE.is_tool_allowed("interpret_image")
