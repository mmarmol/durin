"""The gateway hands the websocket channel what an approval click needs."""
from __future__ import annotations

from durin.bus.queue import MessageBus
from durin.channels.manager import ChannelManager
from durin.config.schema import Config


def test_manager_forwards_the_approval_handles_to_the_websocket_channel():
    def deps():
        return "live handles"

    def turn_key(key: str) -> str:
        return "unified:default"

    manager = ChannelManager(
        Config.model_validate(
            {"channels": {"websocket": {"enabled": True, "allowFrom": ["*"]}}}),
        MessageBus(),
        webui_approval_deps=deps,
        webui_session_turn_key=turn_key,
    )
    channel = manager.channels["websocket"]
    assert channel._approval_deps is deps
    assert channel._session_turn_key is turn_key
