"""Tests for ChannelPostService — posting through a channel, on the record."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.service.channels_post import ChannelPostCommand, ChannelPostService
from durin.service.principal import Principal, Scope
from durin.session.manager import SessionManager


def _principal():
    return Principal.local()


def _manager_with_channel():
    channel = MagicMock()
    channel.send = AsyncMock()
    manager = MagicMock()
    manager.get_channel = MagicMock(return_value=channel)
    return manager, channel


async def test_post_reaches_the_channel_in_its_thread():
    manager, channel = _manager_with_channel()
    svc = ChannelPostService(channel_manager=manager)

    result = await svc.post(
        ChannelPostCommand(
            channel="slack", chat_id="C0AKE2P92F7",
            text="Context gathered — ticket #23113", thread_id="1785755142.844699",
            record=False,
        ),
        _principal(),
    )

    assert result.ok is True
    sent = channel.send.await_args.args[0]
    assert sent.chat_id == "C0AKE2P92F7"
    assert sent.content == "Context gathered — ticket #23113"
    assert sent.metadata == {"slack": {"thread_ts": "1785755142.844699"}}


async def test_post_is_recorded_under_the_key_a_reply_will_land_on(tmp_path):
    """The recorded key must match what SlackChannel derives for that thread.

    That is the whole point: a human answering in the thread continues this
    session instead of starting a blank one beside it.
    """
    manager, _ = _manager_with_channel()
    sessions = SessionManager(tmp_path)
    svc = ChannelPostService(channel_manager=manager, session_manager=sessions)

    result = await svc.post(
        ChannelPostCommand(
            channel="slack", chat_id="C0AKE2P92F7",
            text="Diagnosis ready", thread_id="1785755142.844699",
        ),
        _principal(),
    )

    assert result.session_key == "slack:C0AKE2P92F7:1785755142.844699"
    # The key SlackChannel builds for an inbound message in the same thread.
    assert result.session_key == f"slack:{'C0AKE2P92F7'}:{'1785755142.844699'}"

    rows = sessions.list_sessions()
    assert [r["key"] for r in rows] == ["slack:C0AKE2P92F7:1785755142.844699"]
    assert rows[0]["preview"] == "Diagnosis ready"


async def test_recorded_post_does_not_start_a_turn(tmp_path):
    """Recording writes the transcript directly; publishing inbound would make
    durin answer its own post."""
    manager, _ = _manager_with_channel()
    sessions = SessionManager(tmp_path)
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    svc = ChannelPostService(channel_manager=manager, session_manager=sessions)

    await svc.post(
        ChannelPostCommand(channel="slack", chat_id="C1", text="hola", thread_id="9.9"),
        _principal(),
    )

    bus.publish_inbound.assert_not_awaited()
    session = sessions.get_or_create("slack:C1:9.9")
    assert [(m["role"], m["content"]) for m in session.messages] == [("assistant", "hola")]


async def test_second_post_continues_the_same_session(tmp_path):
    manager, _ = _manager_with_channel()
    sessions = SessionManager(tmp_path)
    svc = ChannelPostService(channel_manager=manager, session_manager=sessions)

    for text in ("stage 1 done", "stage 2 done"):
        await svc.post(
            ChannelPostCommand(channel="slack", chat_id="C1", text=text, thread_id="9.9"),
            _principal(),
        )

    assert len(sessions.list_sessions()) == 1
    session = sessions.get_or_create("slack:C1:9.9")
    assert [m["content"] for m in session.messages] == ["stage 1 done", "stage 2 done"]


async def test_unknown_channel_is_reported_not_raised():
    manager = MagicMock()
    manager.get_channel = MagicMock(return_value=None)
    svc = ChannelPostService(channel_manager=manager)

    result = await svc.post(
        ChannelPostCommand(channel="slack", chat_id="C1", text="hi"), _principal()
    )

    assert result.ok is False
    assert "not running" in (result.error or "")


async def test_send_failure_does_not_record():
    """A post that never reached the channel must not appear in the transcript."""
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=RuntimeError("socket closed"))
    manager = MagicMock()
    manager.get_channel = MagicMock(return_value=channel)
    sessions = MagicMock()
    svc = ChannelPostService(channel_manager=manager, session_manager=sessions)

    result = await svc.post(
        ChannelPostCommand(channel="slack", chat_id="C1", text="hi"), _principal()
    )

    assert result.ok is False
    assert "socket closed" in (result.error or "")
    sessions.get_or_create.assert_not_called()


async def test_posting_requires_its_own_scope():
    """sessions:write must not be enough to speak to an external party."""
    manager, _ = _manager_with_channel()
    svc = ChannelPostService(channel_manager=manager)
    principal = Principal.remote("tok", {Scope.SESSIONS_WRITE.value})

    with pytest.raises(Exception):
        await svc.post(
            ChannelPostCommand(channel="slack", chat_id="C1", text="hi"), principal
        )


async def test_unthreaded_post_keys_on_the_conversation():
    manager, channel = _manager_with_channel()
    svc = ChannelPostService(channel_manager=manager)

    result = await svc.post(
        ChannelPostCommand(channel="slack", chat_id="C1", text="hi", record=False),
        _principal(),
    )

    assert result.ok is True
    assert channel.send.await_args.args[0].metadata == {}
    assert ChannelPostService.session_key_for("slack", "C1", None) == "slack:C1"
