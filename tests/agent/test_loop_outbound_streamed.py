"""Overflow/abort turns must be delivered, not swallowed by `_streamed`.

When a turn aborts BEFORE the model streams anything (context overflow, idle
timeout), the runner sets ``final_content`` to an ``Error: ...`` string. The
loop used to mark *every* non-error/non-tool_error turn ``_streamed=True``,
which tells the channel "already delivered via stream" — so
``ChannelManager._send_once`` skips sending it. Nothing was actually streamed,
so the user saw a silent failure. These turns must NOT be marked streamed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")


def _msg() -> InboundMessage:
    return InboundMessage(channel="websocket", sender_id="u", chat_id="c", content="hi")


@pytest.mark.parametrize("stop_reason", ["mid_turn_precheck_overflow", "circuit_breaker_idle_timeout"])
def test_non_streamed_abort_is_delivered(tmp_path: Path, stop_reason: str) -> None:
    """An abort that never streamed must yield an outbound WITHOUT the
    ``_streamed`` flag, so the channel actually sends the error."""
    loop = _make_loop(tmp_path)
    out = loop._assemble_outbound(
        _msg(),
        "Error: prompt overflow before LLM call (estimated 1 tokens, budget 0).",
        [],
        stop_reason,
        False,
        AsyncMock(),  # on_stream not None → streaming channel
    )
    assert out is not None
    assert not out.metadata.get("_streamed"), "abort error must be delivered, not suppressed"


def test_completed_streaming_turn_is_marked_streamed(tmp_path: Path) -> None:
    """A normal completed turn on a streaming channel WAS streamed, so it stays
    marked ``_streamed`` (the channel must not re-send it)."""
    loop = _make_loop(tmp_path)
    out = loop._assemble_outbound(
        _msg(), "Here is the answer.", [], "completed", False, AsyncMock(),
    )
    assert out is not None
    assert out.metadata.get("_streamed") is True
