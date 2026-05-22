"""Integration: ``_normalize_tool_result`` applies per-block validation.

The unit tests in tests/utils/test_tool_result_validation.py cover the
middleware itself. This file verifies the wiring: tool results returned
from the registry pass through the validator before reaching the LLM
message list.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse, ToolCallRequest
from durin.utils.tool_result_validation import (
    MAX_BLOCK_TEXT_CHARS,
    MAX_IMAGE_BLOCK_BYTES,
)

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


@pytest.mark.asyncio
async def test_runner_drops_oversized_image_block_from_tool_result(tmp_path):
    """A tool returning a list with an oversized data-URL image must have
    that block replaced with a text placeholder before it lands in the
    persisted message history."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    payload = "A" * (MAX_IMAGE_BLOCK_BYTES * 4 // 3 + 4096)
    huge_image_result = [
        {"type": "text", "text": "Here is your image:"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}},
    ]

    provider = MagicMock()
    call_count = {"n": 0}

    async def chat_with_retry(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="call_1", name="image_gen", arguments={})],
                usage={},
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value=huge_image_result)

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "gen an image"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        workspace=tmp_path,
    ))

    assert result.final_content == "done"
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    content = tool_messages[0]["content"]
    # The image block has been replaced with a text placeholder.
    assert isinstance(content, list)
    types = [b.get("type") for b in content]
    assert types == ["text", "text"]
    assert "image dropped" in content[1]["text"]


@pytest.mark.asyncio
async def test_runner_truncates_oversized_text_block_in_tool_result(tmp_path):
    """Use a high aggregate cap so the block validator's effect is visible
    on the persisted message instead of being shadowed by the spillover
    layer (which JSON-stringifies all-text lists and writes to disk)."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    big = "x" * (MAX_BLOCK_TEXT_CHARS + 500)
    tool_output = [
        {"type": "text", "text": "small"},
        {"type": "text", "text": big},
    ]

    provider = MagicMock()
    call_count = {"n": 0}

    async def chat_with_retry(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="call_1", name="some_tool", arguments={})],
                usage={},
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value=tool_output)

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=500_000,  # high enough that spillover doesn't trigger
        workspace=tmp_path,
    ))

    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    content = tool_messages[0]["content"]
    assert isinstance(content, list)
    assert content[0]["text"] == "small"
    assert content[1]["text"].startswith("x" * MAX_BLOCK_TEXT_CHARS)
    assert "block truncated" in content[1]["text"]


# ---------------------------------------------------------------------------
# _coerce_tool_content — a dict result must not become an untyped block
# (regression: z.ai 1214 `content[0].type: cannot be empty`).
# ---------------------------------------------------------------------------


def test_coerce_tool_content_json_encodes_a_dict_result() -> None:
    from durin.agent.runner import AgentRunner

    out = AgentRunner._coerce_tool_content({"results": [], "total": 0})
    assert isinstance(out, str)
    assert '"total": 0' in out


def test_coerce_tool_content_passes_strings_and_typed_blocks() -> None:
    from durin.agent.runner import AgentRunner

    assert AgentRunner._coerce_tool_content("plain text") == "plain text"
    blocks = [{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {}}]
    assert AgentRunner._coerce_tool_content(blocks) is blocks


def test_coerce_tool_content_json_encodes_an_untyped_list() -> None:
    from durin.agent.runner import AgentRunner

    # A list whose items are not typed blocks is not a valid blocks list.
    out = AgentRunner._coerce_tool_content([{"results": []}, {"total": 0}])
    assert isinstance(out, str)


def test_sanitize_empty_content_stringifies_an_untyped_dict() -> None:
    """A tool message whose content is a raw dict must become text, not
    a one-element block list with no `type`."""
    from durin.providers.base import LLMProvider

    msgs = [{"role": "tool", "tool_call_id": "x", "content": {"results": [], "total": 0}}]
    out = LLMProvider._sanitize_empty_content(msgs)
    assert isinstance(out[0]["content"], str)
    assert '"total": 0' in out[0]["content"]

    # A genuine typed block dict is still wrapped into a blocks list.
    msgs2 = [{"role": "user", "content": {"type": "text", "text": "hi"}}]
    out2 = LLMProvider._sanitize_empty_content(msgs2)
    assert out2[0]["content"] == [{"type": "text", "text": "hi"}]
