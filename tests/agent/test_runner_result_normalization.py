"""What the model sees of a tool result, call after call.

A result is normalized when it arrives and again on every later iteration
(the runner re-applies its result budget to the whole history). These tests
drive the real runner with a scripted provider and read the messages the
provider received, so they pin what the model actually saw.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.providers.base import LLMResponse, ToolCallRequest
from durin.utils.helpers import parse_persisted_reference

CAP = 16_000
TOKEN = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


def _scripted_provider(tool_rounds: int) -> tuple[MagicMock, list[list[dict[str, Any]]]]:
    """A provider that calls the ``probe`` tool ``tool_rounds`` times, then
    answers. Every request's messages are recorded as sent."""
    seen: list[list[dict[str, Any]]] = []
    calls = {"n": 0}

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        seen.append([dict(m) for m in kwargs["messages"]])
        calls["n"] += 1
        if calls["n"] <= tool_rounds:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id=f"call_{calls['n']}", name="probe", arguments={})],
                usage={},
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    return provider, seen


def _tools(*results: Any) -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(side_effect=list(results))
    return tools


async def _run(tmp_path: Path | None, tools: MagicMock, tool_rounds: int) -> list[list[dict[str, Any]]]:
    provider, seen = _scripted_provider(tool_rounds)
    await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go"}],
        tools=tools,
        model="test-model",
        max_iterations=tool_rounds + 2,
        max_tool_result_chars=CAP,
        workspace=tmp_path,
        session_key="test:normalization",
    ))
    return seen


def _tool_content(messages: list[dict[str, Any]], call_id: str) -> Any:
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
            return message.get("content")
    raise AssertionError(f"no tool message for {call_id}")


def _skill_like_payload() -> dict[str, Any]:
    """Shaped like ``skill_view``'s dict: the needle sits far past the cap."""
    lines = [f"Line {i}: " + "lorem ipsum dolor sit amet " * 3 for i in range(900)]
    lines[600] = "NEEDLE rule_executed is the exhibit for did rule X run"
    return {
        "name": "big-skill",
        "skill_dir": "/ws/skills/big-skill",
        "content": "\n".join(lines),
        "linked_files": ["scripts/a.py"],
    }


@pytest.mark.asyncio
async def test_an_oversized_dict_result_is_saved_whole_and_readable(tmp_path: Path) -> None:
    seen = await _run(tmp_path, _tools(_skill_like_payload(), "ok"), tool_rounds=2)

    first = _tool_content(seen[1], "call_1")
    ref = parse_persisted_reference(first)
    assert ref is not None, f"expected a persisted reference, got: {str(first)[:200]}"
    path, size = ref
    text = Path(path).read_text(encoding="utf-8")
    assert size == len(text)
    # The dict's own line breaks survive, so the file can be paged by lines.
    assert len(text.splitlines()) > 800
    assert any("NEEDLE rule_executed" in line for line in text.splitlines())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        pytest.param("x" * 40_000, id="oversized-str"),
        pytest.param({"k": "v" * 40_000}, id="oversized-dict"),
        pytest.param({"k": "small"}, id="small-dict"),
        pytest.param(_skill_like_payload(), id="skill-like-dict"),
    ],
)
async def test_a_result_looks_the_same_on_every_later_call(tmp_path: Path, result: Any) -> None:
    seen = await _run(tmp_path, _tools(result, "ok"), tool_rounds=2)

    arrived = _tool_content(seen[1], "call_1")
    later = _tool_content(seen[2], "call_1")
    assert later == arrived
    assert len(arrived) <= CAP


@pytest.mark.asyncio
async def test_without_a_workspace_the_cut_result_still_fits_the_cap() -> None:
    seen = await _run(None, _tools("y" * 40_000, "ok"), tool_rounds=2)

    arrived = _tool_content(seen[1], "call_1")
    assert len(arrived) <= CAP
    assert _tool_content(seen[2], "call_1") == arrived


@pytest.mark.asyncio
async def test_secrets_inside_a_dict_result_never_reach_the_model_or_the_disk(tmp_path: Path) -> None:
    small = {"results": [{"path": "creds.env", "content": f"OPENAI_API_KEY={TOKEN}\n"}]}
    big = {"results": [{"path": "creds.env", "content": f"OPENAI_API_KEY={TOKEN}\n" + "filler line\n" * 3000}]}
    seen = await _run(tmp_path, _tools(small, big, "ok"), tool_rounds=3)

    assert TOKEN not in str(_tool_content(seen[1], "call_1"))
    spilled = _tool_content(seen[2], "call_2")
    assert TOKEN not in spilled
    ref = parse_persisted_reference(spilled)
    assert ref is not None
    saved = Path(ref[0]).read_text(encoding="utf-8")
    assert TOKEN not in saved
    # A string nested inside the result (a batch read's file body) keeps its
    # line breaks on disk too.
    assert len(saved.splitlines()) > 3000


@pytest.mark.asyncio
async def test_an_oversized_text_block_list_is_saved_line_by_line(tmp_path: Path) -> None:
    body = "\n".join(f"row {i} " + "z" * 60 for i in range(600))
    blocks = [{"type": "text", "text": "header"}, {"type": "text", "text": body}]
    seen = await _run(tmp_path, _tools(blocks, "ok"), tool_rounds=2)

    ref = parse_persisted_reference(_tool_content(seen[1], "call_1"))
    assert ref is not None
    lines = Path(ref[0]).read_text(encoding="utf-8").splitlines()
    assert "row 599 " + "z" * 60 in lines
