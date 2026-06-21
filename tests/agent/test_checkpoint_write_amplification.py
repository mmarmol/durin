"""Tests for mid-turn checkpoint write-amplification (Phase B, Task 2).

Before the fix, each call to ``_set_runtime_checkpoint`` triggered a full
``sessions.save(session)`` — writing the ``.jsonl``, regenerating the ``.md``,
and re-indexing FTS.  With N tool iterations per turn that's ~2N extra rewrites.

After the fix, ``_set_runtime_checkpoint`` calls ``sessions.save_runtime_state``
(sidecar-only) so the ``.jsonl`` rewrite count stays ~constant (≈3: early +
final + end-of-turn) regardless of N.

See docs/architecture/concurrency.md for the sidecar split design.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.providers.base import LLMResponse, ToolCallRequest
from durin.session.manager import SessionManager


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )


def _tool_call(i: int) -> ToolCallRequest:
    return ToolCallRequest(id=f"call_{i}", name="read_file", arguments={"path": f"/tmp/f{i}"})


def _tool_responses(n: int) -> list[LLMResponse]:
    """N rounds of tool-call responses followed by a final stop response."""
    responses = [
        LLMResponse(
            content=f"iteration {i}",
            tool_calls=[_tool_call(i)],
            usage={},
        )
        for i in range(n)
    ]
    responses.append(LLMResponse(content="done", tool_calls=[], usage={}, finish_reason="stop"))
    return responses


@pytest.mark.parametrize("n_iterations", [3, 10])
@pytest.mark.asyncio
async def test_jsonl_rewrites_do_not_grow_with_tool_iterations(
    tmp_path: Path, n_iterations: int
) -> None:
    """The number of ``.jsonl`` full rewrites during a turn must be constant
    (early-persist + end-of-turn) and must NOT grow with N tool iterations.

    BEFORE the fix: count grows by ~2 for each extra iteration (once when the
    checkpoint is written, once cleared).
    AFTER the fix: count stays constant because checkpoints go to the sidecar.
    """
    N = n_iterations

    loop = _make_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.prepare_call = MagicMock(return_value=(None, {}, None))
    loop.tools.execute = AsyncMock(return_value="file content")
    loop.provider.chat_with_retry = AsyncMock(side_effect=_tool_responses(N))

    jsonl_replace_count = 0
    _real_replace = os.replace

    def _counting_replace(src: str, dst: str) -> None:
        nonlocal jsonl_replace_count
        if str(dst).endswith(".jsonl"):
            jsonl_replace_count += 1
        _real_replace(src, dst)

    with patch("durin.session.manager.os.replace", side_effect=_counting_replace):
        result = await loop._process_message(
            InboundMessage(channel="cli", sender_id="u1", chat_id="amp-test", content="go")
        )

    assert result is not None, "Turn must complete"
    assert result.content == "done"

    # Expected rewrites: early-persist (user message) + final (assistant) + end-of-turn = 3.
    # Constant across all N values — checkpoints no longer hit the full save path.
    # Would be ~2*N+something without the fix; assert tight bound proves the fix holds.
    assert jsonl_replace_count <= 3, (
        f"jsonl rewrites ({jsonl_replace_count}) with N={N} exceeds constant bound — "
        "checkpoint writes are still hitting the full save path"
    )


@pytest.mark.asyncio
async def test_runtime_checkpoint_survives_process_restart(tmp_path: Path) -> None:
    """A ``runtime_checkpoint`` written via ``_set_runtime_checkpoint`` must be
    readable by a fresh ``SessionManager`` (simulating a process restart) so
    that ``_restore_runtime_checkpoint`` can recover it.

    This proves the sidecar merge-on-load path is exercised by the fix.
    """
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:restart-test")

    payload = {
        "assistant_message": {
            "role": "assistant",
            "content": "working",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        "completed_tool_results": [],
        "pending_tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }

    # Persist the session so the .jsonl exists on disk (required for load).
    loop.sessions.save(session)

    # Write the runtime checkpoint via the same path the loop uses.
    loop._set_runtime_checkpoint(session, payload)

    # Simulate a fresh process: new SessionManager with empty cache.
    fresh_sm = SessionManager(tmp_path)
    fresh_session = fresh_sm.get_or_create("cli:restart-test")

    assert fresh_session.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_KEY) == payload, (
        "runtime_checkpoint must survive a SessionManager reload via the sidecar"
    )

    # Verify that _restore_runtime_checkpoint can consume it.
    fresh_loop = AgentLoop.__new__(AgentLoop)
    from durin.config.schema import AgentDefaults
    fresh_loop.max_tool_result_chars = AgentDefaults().max_tool_result_chars

    restored = fresh_loop._restore_runtime_checkpoint(fresh_session)
    assert restored is True, "_restore_runtime_checkpoint must return True for a valid checkpoint"
    assert fresh_session.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_KEY) is None, (
        "checkpoint key must be consumed (cleared) after restore"
    )
