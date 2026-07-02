"""/pairing: owner-gated wiring of the pairing store command handler."""

from types import SimpleNamespace

import pytest

from durin.command.builtin import cmd_pairing
from durin.command.router import CommandContext


def _ctx(channel="telegram", sender="111", allow_from=("111",), args=""):
    channel_cfg = SimpleNamespace(allow_from=list(allow_from))
    app_config = SimpleNamespace(channels=SimpleNamespace(**{channel: channel_cfg}))
    msg = SimpleNamespace(
        channel=channel, chat_id="c1", sender_id=sender, metadata={},
    )
    loop = SimpleNamespace(app_config=app_config)
    return CommandContext(
        msg=msg, session=None, key="k",
        raw=f"/pairing {args}".strip(), args=args, loop=loop,
    )


@pytest.mark.asyncio
async def test_pairing_list_from_owner(monkeypatch):
    monkeypatch.setattr(
        "durin.pairing.store.list_pending", lambda: [], raising=True
    )
    out = await cmd_pairing(_ctx(args="list"))
    assert "No pending pairing requests" in out.content


@pytest.mark.asyncio
async def test_pairing_denied_for_non_owner_channel_sender():
    out = await cmd_pairing(_ctx(sender="999", allow_from=("111",)))
    assert "owner" in out.content.lower()


@pytest.mark.asyncio
async def test_pairing_always_allowed_from_owner_surfaces(monkeypatch):
    monkeypatch.setattr(
        "durin.pairing.store.list_pending", lambda: [], raising=True
    )
    ctx = _ctx(channel="websocket", sender="anything", allow_from=())
    out = await cmd_pairing(ctx)
    assert "No pending pairing requests" in out.content


@pytest.mark.asyncio
async def test_pairing_allowed_with_dict_shaped_channel_config(monkeypatch):
    """Real AppConfig.channels stores built-in channel configs as plain
    dicts (pydantic extra="allow" fields), not attribute-bearing objects."""
    monkeypatch.setattr(
        "durin.pairing.store.list_pending", lambda: [], raising=True
    )
    channel_cfg = {"allow_from": ["111"]}
    app_config = SimpleNamespace(channels={"telegram": channel_cfg})
    msg = SimpleNamespace(
        channel="telegram", chat_id="c1", sender_id="111", metadata={},
    )
    loop = SimpleNamespace(app_config=app_config)
    ctx = CommandContext(
        msg=msg, session=None, key="k", raw="/pairing list", args="list", loop=loop,
    )
    out = await cmd_pairing(ctx)
    assert "No pending pairing requests" in out.content
