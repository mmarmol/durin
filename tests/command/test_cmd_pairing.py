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


@pytest.mark.asyncio
async def test_pairing_composite_sender_allowed_by_numeric_half(monkeypatch):
    """Composite Telegram sender "555|owner_handle" is allowed if numeric half in allowlist."""
    monkeypatch.setattr(
        "durin.pairing.store.list_pending", lambda: [], raising=True
    )
    out = await cmd_pairing(_ctx(sender="555|owner_handle", allow_from=("555",)))
    assert "No pending pairing requests" in out.content


@pytest.mark.asyncio
async def test_pairing_composite_sender_allowed_by_username_half(monkeypatch):
    """Composite Telegram sender "555|owner_handle" is allowed if username half in allowlist."""
    monkeypatch.setattr(
        "durin.pairing.store.list_pending", lambda: [], raising=True
    )
    out = await cmd_pairing(_ctx(sender="555|owner_handle", allow_from=("owner_handle",)))
    assert "No pending pairing requests" in out.content


@pytest.mark.asyncio
async def test_pairing_composite_sender_denied_for_non_matching_halves(monkeypatch):
    """Composite sender "555|owner_handle" is denied if neither half is in allowlist."""
    out = await cmd_pairing(_ctx(sender="555|owner_handle", allow_from=("999",)))
    assert "owner" in out.content.lower()


@pytest.mark.asyncio
async def test_pairing_malformed_composite_sender_denied(monkeypatch):
    """Malformed composite senders (non-numeric sid, empty username, multiple pipes) are denied."""
    # Non-numeric sid
    out1 = await cmd_pairing(_ctx(sender="abc|owner_handle", allow_from=("abc",)))
    assert "owner" in out1.content.lower()

    # Empty username
    out2 = await cmd_pairing(_ctx(sender="555|", allow_from=("555",)))
    assert "owner" in out2.content.lower()

    # Multiple pipes
    out3 = await cmd_pairing(_ctx(sender="evil|x|y", allow_from=("evil",)))
    assert "owner" in out3.content.lower()
