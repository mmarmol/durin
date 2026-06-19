"""Tests for BaseChannel delegating to TranscriptionService (spec §7)."""

from pathlib import Path

import pytest

from durin.service.transcription import TranscriptResult


class FakeService:
    def __init__(self, text="hi from service"):
        self.text = text
        self.calls = 0

    async def transcribe_and_cache(self, path):
        self.calls += 1
        return TranscriptResult(
            text=self.text,
            cached=False,
            meta_path=None,
            audio_path=Path(path),
        )


@pytest.mark.asyncio
async def test_channel_delegates_to_service(tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    fake = FakeService()

    from durin.channels.base import BaseChannel

    class Ch(BaseChannel):
        name = "test"

        async def start(self):
            ...

        async def stop(self):
            ...

        async def send(self, msg):
            ...

    ch = Ch(config=None, bus=None)
    ch.transcription = fake  # injected
    text = await ch.transcribe_audio(audio)
    assert text == "hi from service"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_channel_falls_back_when_no_service(tmp_path):
    """Legacy path: if no service injected and no api key, return '' (no crash)."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    from durin.channels.base import BaseChannel

    class Ch(BaseChannel):
        name = "test"

        async def start(self):
            ...

        async def stop(self):
            ...

        async def send(self, msg):
            ...

    ch = Ch(config=None, bus=None)
    # No service injected; no api key set.
    text = await ch.transcribe_audio(audio)
    assert text == ""
