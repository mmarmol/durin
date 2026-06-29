"""Tests for the central inbound-authorization gate on ChannelManager."""
from __future__ import annotations

import types

import pytest

import durin.channels.manager as mgr_mod


class _Chan:
    name = "telegram"

    def __init__(self, allowed):
        self._allowed = allowed
        self.sent = []

    def is_allowed(self, sender):
        return self._allowed

    async def send(self, msg):
        self.sent.append(msg)


def _make_manager():
    """Build a minimal ChannelManager via __new__ (skips __init__)."""
    m = mgr_mod.ChannelManager.__new__(mgr_mod.ChannelManager)
    m.channels = {}
    m.bus = types.SimpleNamespace(set_inbound_authorizer=lambda fn: None)
    return m


@pytest.fixture
def mgr():
    return _make_manager()


async def test_unknown_channel_allowed(mgr):
    from durin.bus.events import InboundMessage

    result = await mgr._authorize_inbound(
        InboundMessage(channel="cli", sender_id="u", chat_id="c", content="x")
    )
    assert result is True


async def test_known_allowed(mgr):
    from durin.bus.events import InboundMessage

    mgr.channels["telegram"] = _Chan(True)
    result = await mgr._authorize_inbound(
        InboundMessage(channel="telegram", sender_id="s", chat_id="c", content="x")
    )
    assert result is True


async def test_unauthorized_dm_pairs(mgr, monkeypatch):
    from durin.bus.events import InboundMessage

    ch = _Chan(False)
    mgr.channels["telegram"] = ch
    monkeypatch.setattr(
        "durin.channels.manager.generate_code",
        lambda channel, sender: "AAAA-BBBB",
    )
    ok = await mgr._authorize_inbound(
        InboundMessage(
            channel="telegram", sender_id="s", chat_id="c", content="x", is_dm=True
        )
    )
    assert ok is False
    assert len(ch.sent) == 1
    # the sent message is a real pairing reply carrying the code + meta key
    from durin.pairing import PAIRING_CODE_META_KEY

    sent = ch.sent[0]
    assert sent.metadata.get(PAIRING_CODE_META_KEY) == "AAAA-BBBB"
    assert "AAAA-BBBB" in sent.content


async def test_unauthorized_group_denied(mgr):
    from durin.bus.events import InboundMessage

    ch = _Chan(False)
    mgr.channels["telegram"] = ch
    ok = await mgr._authorize_inbound(
        InboundMessage(
            channel="telegram", sender_id="s", chat_id="c", content="x", is_dm=False
        )
    )
    assert ok is False
    assert len(ch.sent) == 0  # group denial must NOT send a pairing code
