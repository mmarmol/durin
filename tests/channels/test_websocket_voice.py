import json

import pytest

from durin.channels.websocket import WebSocketChannel


class _FakeConn:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, raw: str):
        self.sent.append(raw)

    @property
    def remote(self):
        return "test"


def _voice_channel():
    ch = WebSocketChannel.__new__(WebSocketChannel)
    ch.logger = __import__("loguru").logger
    ch._voice = {}
    ch._subs = {}
    return ch


@pytest.mark.asyncio
async def test_voice_start_creates_session_and_emits_listening():
    ch = _voice_channel()
    conn = _FakeConn()
    ch._subs["c1"] = {conn}
    await ch._dispatch_envelope(conn, "client1", {"type": "voice_start", "chat_id": "c1"})
    assert "c1" in ch._voice
    events = [json.loads(r) for r in conn.sent]
    assert any(e.get("event") == "voice_state" and e.get("state") == "listening" for e in events)


@pytest.mark.asyncio
async def test_voice_stop_removes_session_and_emits_idle():
    ch = _voice_channel()
    conn = _FakeConn()
    ch._subs["c1"] = {conn}
    await ch._dispatch_envelope(conn, "client1", {"type": "voice_start", "chat_id": "c1"})
    await ch._dispatch_envelope(conn, "client1", {"type": "voice_stop", "chat_id": "c1"})
    assert "c1" not in ch._voice
    events = [json.loads(r) for r in conn.sent]
    assert any(e.get("event") == "voice_state" and e.get("state") == "idle" for e in events)


from pathlib import Path
from unittest.mock import AsyncMock

from durin.service.transcription import TranscriptResult


def _data_url(mime: str, payload: bytes = b"OggS") -> str:
    import base64

    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


@pytest.mark.asyncio
async def test_voice_utterance_transcribes_then_enqueues_turn(tmp_path):
    ch = _voice_channel()
    conn = _FakeConn()
    ch._subs["c1"] = {conn}

    saved = tmp_path / "u.wav"
    saved.write_bytes(b"x")
    ch._save_envelope_media = lambda media: ([str(saved)], None)

    class FakeService:
        async def transcribe_and_cache(self, path, on_status=None):
            return TranscriptResult(text="hola durin", cached=False, meta_path=None, audio_path=Path(path))

    ch.transcription = FakeService()
    ch._handle_message = AsyncMock()

    from durin.voice.session import VoiceSession

    ch._voice["c1"] = VoiceSession(chat_id="c1")
    await ch._dispatch_envelope(conn, "client1", {
        "type": "voice_utterance", "chat_id": "c1",
        "media": [{"data_url": _data_url("audio/wav"), "name": "u.wav"}],
    })

    ch._handle_message.assert_awaited_once()
    kwargs = ch._handle_message.await_args.kwargs
    assert kwargs["content"] == "hola durin"
    assert kwargs["chat_id"] == "c1"
    assert kwargs["metadata"]["voice"] is True


@pytest.mark.asyncio
async def test_voice_utterance_empty_transcript_does_not_enqueue(tmp_path):
    ch = _voice_channel()
    conn = _FakeConn()
    ch._subs["c1"] = {conn}
    saved = tmp_path / "u.wav"
    saved.write_bytes(b"x")
    ch._save_envelope_media = lambda media: ([str(saved)], None)

    class Silent:
        async def transcribe_and_cache(self, path, on_status=None):
            return TranscriptResult(text="   ", cached=False, meta_path=None, audio_path=Path(path))

    ch.transcription = Silent()
    ch._handle_message = AsyncMock()
    from durin.voice.session import VoiceSession

    ch._voice["c1"] = VoiceSession(chat_id="c1")
    await ch._dispatch_envelope(conn, "client1", {
        "type": "voice_utterance", "chat_id": "c1",
        "media": [{"data_url": _data_url("audio/wav"), "name": "u.wav"}],
    })
    ch._handle_message.assert_not_awaited()
