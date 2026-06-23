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


import asyncio

from durin.bus.events import OutboundMessage
from durin.config.schema import VoiceConfig
from durin.providers.speech import SpeechAudio
from durin.voice.session import VoiceSession


def _wav_bytes():
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 50)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_send_final_reply_speaks_and_emits_voice_audio(tmp_path, monkeypatch):
    monkeypatch.setattr("durin.channels.websocket.get_media_dir", lambda ch=None: tmp_path)
    ch = _voice_channel()
    ch._media_secret = b"test-secret"
    ch._try_append_webui_transcript = lambda *a, **k: None
    ch.voice_config = VoiceConfig()
    conn = _FakeConn()
    ch._subs["c1"] = {conn}
    ch._voice["c1"] = VoiceSession(chat_id="c1")

    class FakeTTS:
        async def synthesize(self, text, *, voice=None, language=None):
            return SpeechAudio(_wav_bytes(), 22050)

    ch.speech_synthesis = FakeTTS()

    await ch.send(OutboundMessage(channel="websocket", chat_id="c1", content="A short reply."))
    # _speak runs as a background task; let it finish.
    for _ in range(20):
        await asyncio.sleep(0)
        if any(json.loads(r).get("event") == "voice_audio" for r in conn.sent):
            break

    events = [json.loads(r) for r in conn.sent]
    audio = [e for e in events if e.get("event") == "voice_audio"]
    assert audio and audio[0]["url"].startswith("/api/media/")
    assert any(e.get("event") == "voice_state" and e.get("state") == "speaking" for e in events)


@pytest.mark.asyncio
async def test_send_skips_speak_for_progress_breadcrumb(tmp_path):
    ch = _voice_channel()
    ch._try_append_webui_transcript = lambda *a, **k: None
    ch.voice_config = VoiceConfig()
    ch.speech_synthesis = object()  # would explode if used
    conn = _FakeConn()
    ch._subs["c1"] = {conn}
    ch._voice["c1"] = VoiceSession(chat_id="c1")

    await ch.send(OutboundMessage(
        channel="websocket", chat_id="c1", content="thinking…", metadata={"_progress": True}
    ))
    for _ in range(5):
        await asyncio.sleep(0)
    events = [json.loads(r) for r in conn.sent]
    assert not any(e.get("event") == "voice_audio" for e in events)


@pytest.mark.asyncio
async def test_barge_in_cancels_speak_and_listens():
    ch = _voice_channel()
    conn = _FakeConn()
    ch._subs["c1"] = {conn}
    sess = VoiceSession(chat_id="c1")
    ch._voice["c1"] = sess

    async def _block():
        await asyncio.sleep(10)

    sess.speak_task = asyncio.create_task(_block())
    await asyncio.sleep(0)
    await ch._dispatch_envelope(conn, "client1", {"type": "voice_barge_in", "chat_id": "c1"})
    assert sess.speak_task is None
    events = [json.loads(r) for r in conn.sent]
    assert any(e.get("event") == "voice_state" and e.get("state") == "listening" for e in events)


@pytest.mark.asyncio
async def test_read_all_synthesizes_full_text(tmp_path, monkeypatch):
    monkeypatch.setattr("durin.channels.websocket.get_media_dir", lambda ch=None: tmp_path)
    ch = _voice_channel()
    ch._media_secret = b"test-secret"
    ch.voice_config = VoiceConfig()
    conn = _FakeConn()
    ch._subs["c1"] = {conn}
    ch._voice["c1"] = VoiceSession(chat_id="c1")

    captured = {}

    class FakeTTS:
        async def synthesize(self, text, *, voice=None, language=None):
            captured["text"] = text
            return SpeechAudio(_wav_bytes(), 22050)

    ch.speech_synthesis = FakeTTS()
    await ch._dispatch_envelope(conn, "client1", {
        "type": "voice_read_all", "chat_id": "c1", "text": "Full answer with `code` here.",
    })
    for _ in range(20):
        await asyncio.sleep(0)
        if any(json.loads(r).get("event") == "voice_audio" for r in conn.sent):
            break
    assert "code" in captured["text"]  # full speakable text, not a summary


def test_manager_injects_speech_synthesis_and_voice_config():
    import inspect

    import durin.channels.manager as mgr

    src = inspect.getsource(mgr)
    assert "SpeechSynthesisService" in src
    assert "speech_synthesis" in src
    assert "voice_config" in src


@pytest.mark.asyncio
async def test_voice_preview_synthesizes_and_returns_signed_url(tmp_path, monkeypatch):
    monkeypatch.setattr("durin.channels.websocket.get_media_dir", lambda ch=None: tmp_path)
    ch = _voice_channel()
    ch._media_secret = b"test-secret"
    conn = _FakeConn()

    captured = {}

    class FakeTTS:
        async def synthesize(self, text, *, voice=None, language=None):
            captured["voice"] = voice
            captured["language"] = language
            captured["text"] = text
            return SpeechAudio(_wav_bytes(), 22050)

    ch.speech_synthesis = FakeTTS()
    await ch._dispatch_envelope(conn, "client1", {
        "type": "voice_preview", "voice": "F4", "language": "es",
    })
    events = [json.loads(r) for r in conn.sent]
    audio = [e for e in events if e.get("event") == "voice_preview_audio"]
    assert audio and audio[0]["url"].startswith("/api/media/")
    assert audio[0]["mime"] == "audio/wav"
    assert captured["voice"] == "F4"
    assert captured["language"] == "es"
    assert captured["text"]  # a non-empty sample was synthesized


@pytest.mark.asyncio
async def test_voice_preview_without_tts_returns_unavailable():
    ch = _voice_channel()
    ch.speech_synthesis = None
    conn = _FakeConn()
    await ch._dispatch_envelope(conn, "client1", {
        "type": "voice_preview", "voice": "F4", "language": "es",
    })
    events = [json.loads(r) for r in conn.sent]
    audio = [e for e in events if e.get("event") == "voice_preview_audio"]
    assert audio and audio[0].get("error") == "tts_unavailable"
