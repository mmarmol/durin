"""Tests for audio MIME acceptance in the websocket channel."""

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


def test_extract_data_url_mime_tolerates_codecs_param():
    """MediaRecorder emits ``audio/webm;codecs=opus`` — the base MIME must parse
    (regression: the regex used to reject any data URL with media-type params,
    so recorded audio silently failed)."""
    from durin.channels.websocket import _extract_data_url_mime, _UPLOAD_MIME_ALLOWED

    assert _extract_data_url_mime("data:audio/webm;codecs=opus;base64,AAAA") == "audio/webm"
    assert _extract_data_url_mime("data:audio/mp4;codecs=mp4a.40.2;base64,AAAA") == "audio/mp4"
    assert _extract_data_url_mime("data:audio/wav;base64,AAAA") == "audio/wav"
    assert _extract_data_url_mime("data:audio/webm;codecs=opus;base64,AAAA") in _UPLOAD_MIME_ALLOWED
    assert _extract_data_url_mime("data:text/plain,hi") is None


@pytest.mark.asyncio
async def test_audio_transcribe_accepts_webm_codecs_opus(tmp_path, monkeypatch):
    """A recorded ``audio/webm;codecs=opus`` upload must reach transcription
    through the REAL _save_envelope_media (not a stub)."""
    import base64 as _b64

    from durin.channels.websocket import WebSocketChannel
    from durin.service.transcription import TranscriptResult

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    monkeypatch.setattr("durin.channels.websocket.get_media_dir", lambda _ch: media_dir)

    payload = _b64.b64encode(b"\x1aE\xdf\xa3 fake webm bytes").decode()
    data_url = f"data:audio/webm;codecs=opus;base64,{payload}"

    seen: dict[str, str] = {}

    class FakeService:
        async def transcribe_and_cache(self, path, on_status=None):
            seen["path"] = str(path)
            return TranscriptResult(text="hola", cached=False, meta_path=None, audio_path=Path(path))

    ch = WebSocketChannel.__new__(WebSocketChannel)
    ch.transcription = FakeService()
    ch.logger = __import__("loguru").logger

    conn = _FakeConn()
    await ch._dispatch_envelope(conn, "client1", {
        "type": "audio_transcribe", "chat_id": "c1", "request_id": "req-webm",
        "media": [{"data_url": data_url, "name": "recording.webm"}],
    })

    assert "path" in seen, "webm;codecs=opus was rejected before reaching transcription"
    events = [json.loads(r) for r in conn.sent]
    terminal = [e for e in events if e.get("event") == "audio_transcript" and not e.get("status")]
    assert terminal and terminal[0]["transcript"] == "hola"
    assert all(e.get("event") != "error" for e in events)


@pytest.mark.asyncio
async def test_audio_transcribe_rejection_replies_with_request_id():
    """A rejected upload must return as ``audio_transcript`` keyed by request_id
    (not a bare ``error`` event) so the composer chip surfaces the failure
    instead of spinning forever."""
    from durin.channels.websocket import WebSocketChannel

    ch = WebSocketChannel.__new__(WebSocketChannel)
    ch.transcription = object()  # rejection happens before transcription is used
    ch.logger = __import__("loguru").logger

    conn = _FakeConn()
    await ch._dispatch_envelope(conn, "client1", {
        "type": "audio_transcribe", "chat_id": "c1", "request_id": "req-bad",
        "media": [{"data_url": "data:application/zip;base64,AAAA", "name": "x.zip"}],
    })

    events = [json.loads(r) for r in conn.sent]
    rejects = [e for e in events if e.get("event") == "audio_transcript" and e.get("error")]
    assert rejects, f"rejection not sent as audio_transcript: {events}"
    assert rejects[0]["request_id"] == "req-bad"
    assert all(e.get("event") != "error" for e in events)
