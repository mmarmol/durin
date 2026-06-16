"""Tool-hint frame gating: payload-rendering channels always receive them.

``send_tool_hints`` gates chat-TEXT hints (telegram-style "read_file(…)"
lines). Channels that render structured tool payloads (webui panels, plan
cards — durin/agent/user_payloads.py) depend on the START frame arriving
while the tool runs; a blocking ask_user would otherwise show nothing to
answer. The manager must let tool_hint frames through for those channels
regardless of the text-hint flag.
"""

from __future__ import annotations

from durin.bus.queue import MessageBus
from durin.channels.base import BaseChannel
from durin.channels.manager import ChannelManager
from durin.config.schema import Config


class _WsLike(BaseChannel):
    name = "websocket"
    display_name = "WS"

    async def start(self):  # pragma: no cover
        pass

    async def stop(self):  # pragma: no cover
        pass

    async def send(self, msg):  # pragma: no cover
        pass


class _Dumb(BaseChannel):
    name = "telegram"
    display_name = "TG"

    async def start(self):  # pragma: no cover
        pass

    async def stop(self):  # pragma: no cover
        pass

    async def send(self, msg):  # pragma: no cover
        pass


def _manager() -> ChannelManager:
    mgr = ChannelManager(Config(), MessageBus())
    ws = _WsLike({}, mgr.bus)
    tg = _Dumb({}, mgr.bus)
    # Defaults: text tool hints disabled everywhere.
    ws.send_tool_hints = False
    tg.send_tool_hints = False
    mgr.channels["websocket"] = ws
    mgr.channels["telegram"] = tg
    return mgr


def test_payload_rendering_channel_always_gets_tool_hint_frames():
    mgr = _manager()
    assert mgr._should_send_progress("websocket", tool_hint=True) is True


def test_dumb_channel_still_respects_the_text_hint_flag():
    mgr = _manager()
    assert mgr._should_send_progress("telegram", tool_hint=True) is False
    mgr.channels["telegram"].send_tool_hints = True
    assert mgr._should_send_progress("telegram", tool_hint=True) is True


def test_plain_progress_unchanged():
    mgr = _manager()
    assert mgr._should_send_progress("websocket", tool_hint=False) is True
    mgr.channels["websocket"].send_progress = False
    assert mgr._should_send_progress("websocket", tool_hint=False) is False
