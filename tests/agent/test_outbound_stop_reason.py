"""The outbound reply carries why the turn ended.

A direct caller (the cron runner) only sees the ``OutboundMessage``; the
runner's ``stop_reason`` is otherwise dropped in ``_assemble_outbound``, so a
provider failure that became the reply text "Sorry, I encountered an error…"
was indistinguishable from a real answer.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus


def _make_loop() -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())
    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager"):
        return AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)


def test_outbound_metadata_carries_the_turn_stop_reason() -> None:
    loop = _make_loop()
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="hi")

    out = loop._assemble_outbound(
        msg, "Sorry, I encountered an error calling the AI model.", [], "error", False, None,
    )

    assert out is not None
    assert out.metadata["_stop_reason"] == "error"


def test_outbound_metadata_stop_reason_for_a_normal_turn() -> None:
    loop = _make_loop()
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="hi")

    out = loop._assemble_outbound(msg, "Done.", [], "stop", False, None)

    assert out is not None
    assert out.metadata["_stop_reason"] == "stop"
