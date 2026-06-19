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
