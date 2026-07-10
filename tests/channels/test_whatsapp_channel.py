"""Tests for WhatsApp channel outbound media support."""

import asyncio
import json
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.bus.events import OutboundMessage
from durin.channels.whatsapp import (
    ACK_TIMEOUT,
    WhatsAppChannel,
    _load_or_create_bridge_token,
)


def _media_path(name: str) -> str:
    """A media path under the real sandboxed media dir, so it survives the
    channel's containment check on inbound media paths."""
    from durin.config.paths import get_media_dir

    return str(get_media_dir("whatsapp") / name)


def _make_channel() -> WhatsAppChannel:
    bus = MagicMock()
    ch = WhatsAppChannel({"enabled": True}, bus)
    ch._ws = AsyncMock()
    ch._connected = True
    return ch


@pytest.fixture
def whatsapp_channel() -> WhatsAppChannel:
    bus = MagicMock()
    ch = WhatsAppChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    ch._ws = AsyncMock()
    ch._connected = True
    ch._handle_message = AsyncMock()
    return ch


def _outbound(content: str = "", reply_to: str | None = None, media: list[str] | None = None) -> OutboundMessage:
    return OutboundMessage(
        channel="whatsapp",
        chat_id="123@s.whatsapp.net",
        content=content,
        reply_to=reply_to,
        media=media or [],
    )


def _ack_all(channel):
    """Auto-ack every frame the test sends (simulates a healthy bridge)."""
    async def _send(raw):
        frame = json.loads(raw)
        if frame["type"] in ("send", "send_media"):
            fut = channel._pending_acks[frame["id"]]
            fut.set_result({"ok": True})
    channel._ws.send.side_effect = _send


@pytest.mark.asyncio
async def test_send_text_only():
    ch = _make_channel()
    _ack_all(ch)
    msg = OutboundMessage(channel="whatsapp", chat_id="123@s.whatsapp.net", content="hello")

    await ch.send(msg)

    payload = json.loads(ch._ws.send.call_args_list[0].args[0])
    assert payload["type"] == "send"
    assert payload["text"] == "hello"


@pytest.mark.asyncio
async def test_send_media_dispatches_send_media_command():
    ch = _make_channel()
    _ack_all(ch)
    msg = OutboundMessage(
        channel="whatsapp",
        chat_id="123@s.whatsapp.net",
        content="check this out",
        media=["/tmp/photo.jpg"],
    )

    await ch.send(msg)

    # Text chunk, media, then a trailing fire-and-forget "typing: paused" frame.
    assert ch._ws.send.call_count == 3
    text_payload = json.loads(ch._ws.send.call_args_list[0][0][0])
    media_payload = json.loads(ch._ws.send.call_args_list[1][0][0])

    assert text_payload["type"] == "send"
    assert text_payload["text"] == "check this out"

    assert media_payload["type"] == "send_media"
    assert media_payload["filePath"] == "/tmp/photo.jpg"
    assert media_payload["mimetype"] == "image/jpeg"
    assert media_payload["fileName"] == "photo.jpg"


@pytest.mark.asyncio
async def test_send_media_only_no_text():
    ch = _make_channel()
    _ack_all(ch)
    msg = OutboundMessage(
        channel="whatsapp",
        chat_id="123@s.whatsapp.net",
        content="",
        media=["/tmp/doc.pdf"],
    )

    await ch.send(msg)

    payload = json.loads(ch._ws.send.call_args_list[0].args[0])
    assert payload["type"] == "send_media"
    assert payload["mimetype"] == "application/pdf"


@pytest.mark.asyncio
async def test_send_multiple_media():
    ch = _make_channel()
    _ack_all(ch)
    msg = OutboundMessage(
        channel="whatsapp",
        chat_id="123@s.whatsapp.net",
        content="",
        media=["/tmp/a.png", "/tmp/b.mp4"],
    )

    await ch.send(msg)

    # Two media sends, then a trailing fire-and-forget "typing: paused" frame.
    assert ch._ws.send.call_count == 3
    p1 = json.loads(ch._ws.send.call_args_list[0][0][0])
    p2 = json.loads(ch._ws.send.call_args_list[1][0][0])
    assert p1["mimetype"] == "image/png"
    assert p2["mimetype"] == "video/mp4"


@pytest.mark.asyncio
async def test_send_when_disconnected_raises():
    ch = _make_channel()
    ch._connected = False

    msg = OutboundMessage(
        channel="whatsapp",
        chat_id="123@s.whatsapp.net",
        content="hello",
        media=["/tmp/x.jpg"],
    )
    with pytest.raises(RuntimeError):
        await ch.send(msg)

    ch._ws.send.assert_not_called()


@pytest.mark.asyncio
async def test_group_policy_mention_skips_unmentioned_group_message():
    ch = WhatsAppChannel({"enabled": True, "allowFrom": ["*"], "groupPolicy": "mention"}, MagicMock())
    ch._handle_message = AsyncMock()

    await ch._handle_bridge_message(
        json.dumps(
            {
                "type": "message",
                "id": "m1",
                "sender": "12345@g.us",
                "pn": "user@s.whatsapp.net",
                "content": "hello group",
                "timestamp": 1,
                "isGroup": True,
                "wasMentioned": False,
            }
        )
    )

    ch._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_group_policy_mention_accepts_mentioned_group_message():
    ch = WhatsAppChannel({"enabled": True, "allowFrom": ["*"], "groupPolicy": "mention"}, MagicMock())
    ch._handle_message = AsyncMock()

    await ch._handle_bridge_message(
        json.dumps(
            {
                "type": "message",
                "id": "m1",
                "sender": "12345@g.us",
                "pn": "user@s.whatsapp.net",
                "content": "hello @bot",
                "timestamp": 1,
                "isGroup": True,
                "wasMentioned": True,
            }
        )
    )

    ch._handle_message.assert_awaited_once()
    kwargs = ch._handle_message.await_args.kwargs
    assert kwargs["chat_id"] == "12345@g.us"
    assert kwargs["sender_id"] == "user"


@pytest.mark.asyncio
async def test_sender_id_prefers_phone_jid_over_lid():
    """sender_id should resolve to phone number when @s.whatsapp.net JID is present."""
    ch = WhatsAppChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())
    ch._handle_message = AsyncMock()

    await ch._handle_bridge_message(
        json.dumps({
            "type": "message",
            "id": "lid1",
            "sender": "ABC123@lid.whatsapp.net",
            "pn": "5551234@s.whatsapp.net",
            "content": "hi",
            "timestamp": 1,
        })
    )

    kwargs = ch._handle_message.await_args.kwargs
    assert kwargs["sender_id"] == "5551234"


@pytest.mark.asyncio
async def test_sender_id_prefers_phone_jid_over_bare_lid():
    """whatsmeow emits the bare '@lid' server form (not '@lid.whatsapp.net');
    it must still be classified as LID, not swallowed by the phone branch."""
    ch = WhatsAppChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())
    ch._handle_message = AsyncMock()

    await ch._handle_bridge_message(
        json.dumps({
            "type": "message",
            "id": "bare-lid-1",
            "sender": "456@lid",
            "pn": "123@s.whatsapp.net",
            "content": "hi",
            "timestamp": 1,
        })
    )

    kwargs = ch._handle_message.await_args.kwargs
    assert kwargs["sender_id"] == "123"
    assert ch._lid_to_phone.get("456") == "123"


@pytest.mark.asyncio
async def test_bare_lid_cache_resolves_lid_only_messages():
    """A follow-up frame carrying only the bare '@lid' sender resolves via
    the cache populated by an earlier frame that also carried the phone."""
    ch = WhatsAppChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())
    ch._handle_message = AsyncMock()

    await ch._handle_bridge_message(
        json.dumps({
            "type": "message",
            "id": "bare-lid-2",
            "sender": "456@lid",
            "pn": "123@s.whatsapp.net",
            "content": "first",
            "timestamp": 1,
        })
    )
    await ch._handle_bridge_message(
        json.dumps({
            "type": "message",
            "id": "bare-lid-3",
            "sender": "456@lid",
            "pn": "",
            "content": "second",
            "timestamp": 2,
        })
    )

    second_kwargs = ch._handle_message.await_args_list[1].kwargs
    assert second_kwargs["sender_id"] == "123"


@pytest.mark.asyncio
async def test_lid_to_phone_cache_resolves_lid_only_messages():
    """When only LID is present, a cached LID→phone mapping should be used."""
    ch = WhatsAppChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())
    ch._handle_message = AsyncMock()

    # First message: both phone and LID → builds cache
    await ch._handle_bridge_message(
        json.dumps({
            "type": "message",
            "id": "c1",
            "sender": "LID99@lid.whatsapp.net",
            "pn": "5559999@s.whatsapp.net",
            "content": "first",
            "timestamp": 1,
        })
    )
    # Second message: only LID, no phone
    await ch._handle_bridge_message(
        json.dumps({
            "type": "message",
            "id": "c2",
            "sender": "LID99@lid.whatsapp.net",
            "pn": "",
            "content": "second",
            "timestamp": 2,
        })
    )

    second_kwargs = ch._handle_message.await_args_list[1].kwargs
    assert second_kwargs["sender_id"] == "5559999"


@pytest.mark.asyncio
async def test_voice_message_transcription_uses_media_path():
    """Voice messages are transcribed when media path is available."""
    ch = WhatsAppChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())
    ch.transcription_provider = "openai"
    ch.transcription_api_key = "sk-test"
    ch._handle_message = AsyncMock()
    ch.transcribe_audio = AsyncMock(return_value="Hello world")
    voice_path = _media_path("voice.ogg")

    await ch._handle_bridge_message(
        json.dumps({
            "type": "message",
            "id": "v1",
            "sender": "12345@s.whatsapp.net",
            "pn": "",
            "content": "[Voice Message]",
            "timestamp": 1,
            "media": [voice_path],
        })
    )

    ch.transcribe_audio.assert_awaited_once_with(voice_path)
    kwargs = ch._handle_message.await_args.kwargs
    assert kwargs["content"].startswith("Hello world")


@pytest.mark.asyncio
async def test_voice_message_transcribes_regardless_of_allow_from() -> None:
    """Pure-transport: the channel does not gate on allow_from locally — the
    central bus gate owns authorization, so an unlisted sender's voice
    message is still transcribed and forwarded."""
    ch = WhatsAppChannel({"enabled": True, "allowFrom": ["allowed"]}, MagicMock())
    ch._handle_message = AsyncMock()
    ch.transcribe_audio = AsyncMock(return_value="Hello world")
    voice_path = _media_path("voice.ogg")

    await ch._handle_bridge_message(
        json.dumps({
            "type": "message",
            "id": "v-blocked",
            "sender": "blocked@s.whatsapp.net",
            "pn": "",
            "content": "[Voice Message]",
            "timestamp": 1,
            "media": [voice_path],
        })
    )

    ch.transcribe_audio.assert_awaited_once_with(voice_path)
    ch._handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_message_no_media_shows_not_available():
    """Voice messages without media produce a fallback placeholder."""
    ch = WhatsAppChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())
    ch._handle_message = AsyncMock()

    await ch._handle_bridge_message(
        json.dumps({
            "type": "message",
            "id": "v2",
            "sender": "12345@s.whatsapp.net",
            "pn": "",
            "content": "[Voice Message]",
            "timestamp": 1,
        })
    )

    kwargs = ch._handle_message.await_args.kwargs
    assert kwargs["content"] == "[Voice Message: Audio not available]"


def test_load_or_create_bridge_token_persists_generated_secret(tmp_path):
    token_path = tmp_path / "whatsapp-auth" / "bridge-token"

    first = _load_or_create_bridge_token(token_path)
    second = _load_or_create_bridge_token(token_path)

    assert first == second
    assert token_path.read_text(encoding="utf-8") == first
    assert len(first) >= 32
    if os.name != "nt":
        assert token_path.stat().st_mode & 0o777 == 0o600


def test_configured_bridge_token_skips_local_token_file(monkeypatch, tmp_path):
    token_path = tmp_path / "whatsapp-auth" / "bridge-token"
    monkeypatch.setattr("durin.channels.whatsapp._bridge_token_path", lambda: token_path)
    ch = WhatsAppChannel({"enabled": True, "bridgeToken": "manual-secret"}, MagicMock())

    assert ch._effective_bridge_token() == "manual-secret"
    assert not token_path.exists()


class FakeQrProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_login_exports_effective_bridge_token(monkeypatch, tmp_path):
    token_path = tmp_path / "whatsapp-auth" / "bridge-token"
    binary_path = tmp_path / "durin-whatsapp-bridge"
    calls = []

    monkeypatch.setattr("durin.channels.whatsapp._bridge_token_path", lambda: token_path)

    async def fake_ensure_bridge_binary():
        return binary_path

    monkeypatch.setattr("durin.channels.whatsapp_bridge.ensure_bridge_binary", fake_ensure_bridge_binary)

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeQrProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    ch = WhatsAppChannel({"enabled": True}, MagicMock())

    assert await ch.login() is True
    assert len(calls) == 1

    args, kwargs = calls[0]
    assert args == (str(binary_path), "qr", "--auth-dir", str(token_path.parent))
    assert kwargs["env"]["BRIDGE_TOKEN"] == token_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_login_force_deletes_existing_session_db(monkeypatch, tmp_path):
    token_path = tmp_path / "whatsapp-auth" / "bridge-token"
    token_path.parent.mkdir(parents=True)
    db_path = token_path.parent / "whatsmeow.db"
    db_path.write_text("stale-session")

    monkeypatch.setattr("durin.channels.whatsapp._bridge_token_path", lambda: token_path)

    async def fake_ensure_bridge_binary():
        return tmp_path / "durin-whatsapp-bridge"

    monkeypatch.setattr("durin.channels.whatsapp_bridge.ensure_bridge_binary", fake_ensure_bridge_binary)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeQrProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    ch = WhatsAppChannel({"enabled": True}, MagicMock())

    assert await ch.login(force=True) is True
    assert not db_path.exists()


@pytest.mark.asyncio
async def test_login_returns_false_when_binary_setup_fails(monkeypatch):
    from durin.channels.whatsapp_bridge import BridgeSetupError

    async def fake_ensure_bridge_binary():
        raise BridgeSetupError("no build for this platform")

    monkeypatch.setattr("durin.channels.whatsapp_bridge.ensure_bridge_binary", fake_ensure_bridge_binary)
    ch = WhatsAppChannel({"enabled": True}, MagicMock())

    assert await ch.login() is False


class FakeSupervisor:
    """Stand-in for BridgeSupervisor: no real process, never needs login."""

    def __init__(self, *args, **kwargs) -> None:
        self.needs_login = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_start_sends_auth_message_with_generated_token(monkeypatch, tmp_path):
    token_path = tmp_path / "whatsapp-auth" / "bridge-token"
    sent_messages: list[str] = []

    class FakeWS:
        def __init__(self) -> None:
            self.close = AsyncMock()

        async def send(self, message: str) -> None:
            sent_messages.append(message)
            ch._running = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeConnect:
        def __init__(self, ws):
            self.ws = ws

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_ensure_bridge_binary():
        return tmp_path / "durin-whatsapp-bridge"

    monkeypatch.setattr("durin.channels.whatsapp._bridge_token_path", lambda: token_path)
    monkeypatch.setattr("durin.channels.whatsapp_bridge.ensure_bridge_binary", fake_ensure_bridge_binary)
    monkeypatch.setattr("durin.channels.whatsapp_bridge.BridgeSupervisor", FakeSupervisor)
    monkeypatch.setitem(
        sys.modules,
        "websockets",
        types.SimpleNamespace(connect=lambda url: FakeConnect(FakeWS())),
    )

    ch = WhatsAppChannel({"enabled": True, "bridgeUrl": "ws://localhost:3001"}, MagicMock())
    await ch.start()

    assert sent_messages == [
        json.dumps({"type": "auth", "token": token_path.read_text(encoding="utf-8")})
    ]


class TestSendAcks:
    @pytest.mark.asyncio
    async def test_send_waits_for_ack(self, whatsapp_channel):
        assert ACK_TIMEOUT == 30.0
        _ack_all(whatsapp_channel)
        await whatsapp_channel.send(_outbound(content="hola"))
        sent = json.loads(whatsapp_channel._ws.send.call_args_list[0].args[0])
        assert sent["type"] == "send" and "id" in sent

    @pytest.mark.asyncio
    async def test_nack_raises(self, whatsapp_channel):
        async def _send(raw):
            frame = json.loads(raw)
            if frame["type"] == "send":
                whatsapp_channel._pending_acks[frame["id"]].set_result(
                    {"ok": False, "error": "boom"})
        whatsapp_channel._ws.send.side_effect = _send
        with pytest.raises(RuntimeError, match="boom"):
            await whatsapp_channel.send(_outbound(content="hola"))

    @pytest.mark.asyncio
    async def test_not_connected_raises(self, whatsapp_channel):
        whatsapp_channel._connected = False
        with pytest.raises(RuntimeError):
            await whatsapp_channel.send(_outbound(content="hola"))


class TestSendFormatting:
    @pytest.mark.asyncio
    async def test_markdown_converted(self, whatsapp_channel):
        _ack_all(whatsapp_channel)
        await whatsapp_channel.send(_outbound(content="**hola**"))
        sent = json.loads(whatsapp_channel._ws.send.call_args_list[0].args[0])
        assert sent["text"] == "*hola*"

    @pytest.mark.asyncio
    async def test_long_message_chunked(self, whatsapp_channel):
        _ack_all(whatsapp_channel)
        await whatsapp_channel.send(_outbound(content="x" * 9000))
        sends = [json.loads(c.args[0]) for c in whatsapp_channel._ws.send.call_args_list
                 if json.loads(c.args[0])["type"] == "send"]
        assert len(sends) >= 3

    @pytest.mark.asyncio
    async def test_reply_to_on_first_chunk_only(self, whatsapp_channel):
        _ack_all(whatsapp_channel)
        await whatsapp_channel.send(_outbound(content="x" * 9000, reply_to="MSGID"))
        sends = [json.loads(c.args[0]) for c in whatsapp_channel._ws.send.call_args_list
                 if json.loads(c.args[0])["type"] == "send"]
        assert sends[0]["reply_to"] == "MSGID"
        assert all("reply_to" not in s for s in sends[1:])


class TestInboundV2:
    @pytest.mark.asyncio
    async def test_voice_flag_triggers_transcription(self, whatsapp_channel):
        whatsapp_channel._handle_message = AsyncMock()
        whatsapp_channel.transcribe_audio = AsyncMock(return_value="Hello world")
        voice_path = _media_path("v.ogg")

        await whatsapp_channel._handle_bridge_message(
            json.dumps({
                "type": "message",
                "id": "voice-v2",
                "sender": "12345@s.whatsapp.net",
                "pn": "",
                "content": "",
                "voice": True,
                "media": [voice_path],
                "timestamp": 1,
            })
        )

        whatsapp_channel.transcribe_audio.assert_awaited_once_with(voice_path)
        kwargs = whatsapp_channel._handle_message.await_args.kwargs
        assert kwargs["content"].startswith("Hello world")

    @pytest.mark.asyncio
    async def test_quoted_passed_in_metadata(self, whatsapp_channel):
        whatsapp_channel._handle_message = AsyncMock()
        quoted = {"id": "Q", "sender": "1@s.whatsapp.net", "text": "orig"}

        await whatsapp_channel._handle_bridge_message(
            json.dumps({
                "type": "message",
                "id": "m-quoted",
                "sender": "12345@s.whatsapp.net",
                "pn": "",
                "content": "reply text",
                "quoted": quoted,
                "timestamp": 1,
            })
        )

        kwargs = whatsapp_channel._handle_message.await_args.kwargs
        assert kwargs["metadata"]["quoted"] == quoted

    @pytest.mark.asyncio
    async def test_typing_composing_sent_on_accept(self, whatsapp_channel):
        whatsapp_channel._handle_message = AsyncMock()

        await whatsapp_channel._handle_bridge_message(
            json.dumps({
                "type": "message",
                "id": "m-typing",
                "sender": "12345@s.whatsapp.net",
                "pn": "",
                "content": "hi",
                "timestamp": 1,
            })
        )

        sent = [json.loads(c.args[0]) for c in whatsapp_channel._ws.send.call_args_list]
        assert {"type": "typing", "to": "12345@s.whatsapp.net", "state": "composing"} in sent


class TestPureTransport:
    @pytest.mark.asyncio
    async def test_unknown_sender_still_published_with_is_dm(self, whatsapp_channel):
        """The channel must NOT pre-filter senders; the central bus gate
        handles pairing. DMs must carry is_dm=True."""
        whatsapp_channel.config.allow_from = []  # nobody allowed locally
        frame = {"type": "message", "sender": "999@s.whatsapp.net",
                 "content": "hola", "id": "M1", "isGroup": False}
        await whatsapp_channel._handle_bridge_message(json.dumps(frame))
        whatsapp_channel._handle_message.assert_called_once()
        kwargs = whatsapp_channel._handle_message.call_args.kwargs
        assert kwargs["is_dm"] is True

    @pytest.mark.asyncio
    async def test_group_message_not_dm(self, whatsapp_channel):
        frame = {"type": "message", "sender": "123@g.us", "pn": "5@s.whatsapp.net",
                 "content": "hola", "id": "M2", "isGroup": True}
        await whatsapp_channel._handle_bridge_message(json.dumps(frame))
        assert whatsapp_channel._handle_message.call_args.kwargs["is_dm"] is False

    @pytest.mark.asyncio
    async def test_media_path_outside_media_dir_is_dropped(self, whatsapp_channel):
        """A path-traversal bridge (or a compromised/legacy one) could report
        a media path outside our sandboxed media dir; the channel must drop
        it rather than hand it to the agent as a readable attachment."""
        await whatsapp_channel._handle_bridge_message(
            json.dumps({
                "type": "message",
                "id": "trav1",
                "sender": "12345@s.whatsapp.net",
                "pn": "",
                "content": "hi",
                "timestamp": 1,
                "media": ["/etc/passwd"],
            })
        )

        kwargs = whatsapp_channel._handle_message.await_args.kwargs
        assert kwargs["media"] == []
        assert kwargs["content"] == "hi"


class TestSupervisorLifecycle:
    @pytest.mark.asyncio
    async def test_needs_login_leaves_channel_not_running(self, monkeypatch, tmp_path):
        """When the supervisor reports needs_login, start() must return with
        is_running False so ChannelManager's rebuild path can restart the
        channel after the operator re-pairs."""
        token_path = tmp_path / "whatsapp-auth" / "bridge-token"
        monkeypatch.setattr("durin.channels.whatsapp._bridge_token_path", lambda: token_path)

        async def fake_ensure_bridge_binary():
            return tmp_path / "durin-whatsapp-bridge"

        monkeypatch.setattr("durin.channels.whatsapp_bridge.ensure_bridge_binary", fake_ensure_bridge_binary)

        class NeedsLoginSupervisor(FakeSupervisor):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__()
                self.needs_login = True
                self.stopped = False

            async def stop(self) -> None:
                self.stopped = True

        created: list[NeedsLoginSupervisor] = []

        def make_supervisor(*args, **kwargs):
            sup = NeedsLoginSupervisor()
            created.append(sup)
            return sup

        monkeypatch.setattr("durin.channels.whatsapp_bridge.BridgeSupervisor", make_supervisor)

        ch = WhatsAppChannel({"enabled": True, "bridgeUrl": "ws://localhost:3001"}, MagicMock())
        await ch.start()

        assert ch.is_running is False
        assert created[0].stopped is True
        assert ch._supervisor is None

    @pytest.mark.asyncio
    async def test_supervisor_media_dir_is_sandboxed_media_dir(self, monkeypatch, tmp_path):
        """The bridge's inbound media dir must live under the allowlisted
        media root (get_media_dir), like every other channel, so agent tools
        can read WhatsApp attachments."""
        from durin.config.paths import get_media_dir

        token_path = tmp_path / "whatsapp-auth" / "bridge-token"
        monkeypatch.setattr("durin.channels.whatsapp._bridge_token_path", lambda: token_path)

        async def fake_ensure_bridge_binary():
            return tmp_path / "durin-whatsapp-bridge"

        monkeypatch.setattr("durin.channels.whatsapp_bridge.ensure_bridge_binary", fake_ensure_bridge_binary)

        captured_kwargs: dict = {}

        class RecordingSupervisor(FakeSupervisor):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__()
                captured_kwargs.update(kwargs)
                self.needs_login = True  # end start() immediately

        monkeypatch.setattr("durin.channels.whatsapp_bridge.BridgeSupervisor", RecordingSupervisor)

        ch = WhatsAppChannel({"enabled": True, "bridgeUrl": "ws://localhost:3001"}, MagicMock())
        await ch.start()

        assert captured_kwargs["media_dir"] == get_media_dir("whatsapp")


class TestBackoff:
    def test_backoff_progression(self):
        from durin.channels.whatsapp import _next_backoff
        d = 2.0
        seen = []
        # factor=1.6 needs 7 applications from 2.0 to actually hit the 30.0
        # cap (2.0 -> 3.2 -> 5.12 -> 8.192 -> 13.1072 -> 20.97152 -> 30.0).
        for _ in range(7):
            seen.append(d)
            d = _next_backoff(d)
        assert seen[0] == 2.0
        assert seen[-1] == 30.0  # capped
        assert all(b > a or b == 30.0 for a, b in zip(seen, seen[1:]))


class TestReplyParticipantCache:
    @pytest.mark.asyncio
    async def test_group_reply_includes_known_participant(self, whatsapp_channel):
        _ack_all(whatsapp_channel)
        await whatsapp_channel._handle_bridge_message(
            json.dumps({
                "type": "message",
                "id": "Q1",
                "sender": "12345@g.us",
                "pn": "555@s.whatsapp.net",
                "content": "hi",
                "isGroup": True,
                "timestamp": 1,
            })
        )

        msg = OutboundMessage(channel="whatsapp", chat_id="12345@g.us", content="reply", reply_to="Q1")
        await whatsapp_channel.send(msg)

        # index 0 is the "composing" typing frame fired while handling the
        # inbound message above; the reply is the "send" frame after it.
        sends = [json.loads(c.args[0]) for c in whatsapp_channel._ws.send.call_args_list
                 if json.loads(c.args[0])["type"] == "send"]
        assert sends[0]["reply_to"] == "Q1"
        assert sends[0]["reply_to_participant"] == "555@s.whatsapp.net"

    @pytest.mark.asyncio
    async def test_reply_without_known_participant_omits_field(self, whatsapp_channel):
        _ack_all(whatsapp_channel)
        msg = OutboundMessage(channel="whatsapp", chat_id="12345@g.us", content="reply", reply_to="UNKNOWN")
        await whatsapp_channel.send(msg)

        sent = json.loads(whatsapp_channel._ws.send.call_args_list[0].args[0])
        assert "reply_to_participant" not in sent
