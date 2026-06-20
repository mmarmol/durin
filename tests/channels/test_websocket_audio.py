"""Tests for audio MIME acceptance in the websocket channel (spec §5.1/§6)."""

import base64


def _data_url(mime: str, payload: bytes = b"OgGS") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def test_audio_mime_allowed_in_upload_whitelist():
    from durin.channels.websocket import _UPLOAD_MIME_ALLOWED

    for m in (
        "audio/mpeg",
        "audio/ogg",
        "audio/webm",
        "audio/wav",
        "audio/x-m4a",
        "audio/aac",
        "audio/flac",
    ):
        assert m in _UPLOAD_MIME_ALLOWED, f"{m} not accepted"


def test_audio_mime_served_by_media_endpoint():
    from durin.channels.websocket import _MEDIA_ALLOWED_MIMES

    # Audio must be servable back to the browser for playback.
    for m in ("audio/mpeg", "audio/ogg", "audio/webm", "audio/wav"):
        assert m in _MEDIA_ALLOWED_MIMES


def test_audio_size_cap_is_25mb():
    from durin.channels.websocket import _MAX_AUDIO_BYTES

    assert _MAX_AUDIO_BYTES == 25 * 1024 * 1024


import json
from pathlib import Path

import pytest


class _FakeConn:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, raw: str):
        self.sent.append(raw)

    @property
    def remote(self):
        return "test"


@pytest.mark.asyncio
async def test_audio_transcribe_envelope_returns_transcript(tmp_path):
    """An audio_transcribe envelope stores the audio and replies with the transcript."""
    from durin.channels.websocket import WebSocketChannel
    from durin.service.transcription import TranscriptResult

    audio_bytes = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8
    data_url = _data_url("audio/wav", audio_bytes)
    saved_path = tmp_path / "a.wav"
    saved_path.write_bytes(audio_bytes)

    class FakeService:
        async def transcribe_and_cache(self, path, on_status=None):
            return TranscriptResult(
                text="hello from audio",
                cached=False,
                meta_path=None,
                audio_path=Path(path),
            )

    ch = WebSocketChannel.__new__(WebSocketChannel)
    ch.transcription = FakeService()
    ch.logger = __import__("loguru").logger
    ch._save_envelope_media = lambda media: ([str(saved_path)], None)

    conn = _FakeConn()
    envelope = {
        "type": "audio_transcribe",
        "chat_id": "c1",
        "request_id": "req-123",
        "media": [{"data_url": data_url, "name": "a.wav"}],
    }
    await ch._dispatch_envelope(conn, "client1", envelope)

    events = [json.loads(raw) for raw in conn.sent]
    transcripts = [
        e for e in events
        if e.get("event") == "audio_transcript" and not e.get("status")
    ]
    assert len(transcripts) == 1
    assert transcripts[0]["transcript"] == "hello from audio"
    assert transcripts[0]["name"] == "a.wav"
    assert transcripts[0]["request_id"] == "req-123"


@pytest.mark.asyncio
async def test_audio_transcribe_envelope_no_service_replies_disabled(tmp_path):
    """When no TranscriptionService is wired, the reply flags error=disabled."""
    from durin.channels.websocket import WebSocketChannel

    audio_bytes = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8
    data_url = _data_url("audio/wav", audio_bytes)
    saved_path = tmp_path / "a.wav"
    saved_path.write_bytes(audio_bytes)

    ch = WebSocketChannel.__new__(WebSocketChannel)
    # No transcription attribute -> getattr returns None.
    ch.logger = __import__("loguru").logger
    ch._save_envelope_media = lambda media: ([str(saved_path)], None)

    conn = _FakeConn()
    envelope = {
        "type": "audio_transcribe",
        "chat_id": "c1",
        "request_id": "req-456",
        "media": [{"data_url": data_url, "name": "a.wav"}],
    }
    await ch._dispatch_envelope(conn, "client1", envelope)

    events = [json.loads(raw) for raw in conn.sent]
    transcripts = [
        e for e in events
        if e.get("event") == "audio_transcript" and not e.get("status")
    ]
    assert len(transcripts) == 1
    assert transcripts[0]["error"] == "disabled"
    assert transcripts[0]["transcript"] == ""
    assert transcripts[0]["request_id"] == "req-456"


@pytest.mark.asyncio
async def test_audio_transcribe_forwards_phase_events(tmp_path):
    """Phase callbacks from the provider are forwarded as audio_transcript status events."""
    import asyncio
    from pathlib import Path

    from durin.channels.websocket import WebSocketChannel
    from durin.service.transcription import TranscriptResult

    audio_bytes = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8
    data_url = _data_url("audio/wav", audio_bytes)
    saved_path = tmp_path / "a.wav"
    saved_path.write_bytes(audio_bytes)

    class FakeService:
        async def transcribe_and_cache(self, path, on_status=None):
            if on_status:
                await asyncio.to_thread(on_status, "loading", 0, 0)
                await asyncio.to_thread(on_status, "transcribing", 0, 0)
            return TranscriptResult(
                text="phase test",
                cached=False,
                meta_path=None,
                audio_path=Path(path),
            )

    ch = WebSocketChannel.__new__(WebSocketChannel)
    ch.transcription = FakeService()
    ch.logger = __import__("loguru").logger
    ch._save_envelope_media = lambda media: ([str(saved_path)], None)

    conn = _FakeConn()
    envelope = {
        "type": "audio_transcribe",
        "chat_id": "c1",
        "request_id": "req-phases",
        "media": [{"data_url": data_url, "name": "a.wav"}],
    }
    await ch._dispatch_envelope(conn, "client1", envelope)
    for _ in range(200):  # up to ~2s, exits early as soon as present
        statuses = {json.loads(r).get("status") for r in conn.sent}
        if {"loading", "transcribing"} <= statuses:
            break
        await asyncio.sleep(0.01)

    assert "loading" in statuses
    assert "transcribing" in statuses
