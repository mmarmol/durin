"""Text-to-speech providers (mirrors providers/transcription.py).

The module imports only always-present deps; the local Supertonic backend is
imported lazily inside ``LocalSupertonicProvider`` so this module loads fine in
environments without the ``[tts]`` extra (e.g. CI).
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx
from loguru import logger


@dataclass
class SpeechAudio:
    """Synthesized speech: a complete WAV container plus its sample rate."""

    data: bytes
    sample_rate: int
    format: str = "wav"


def _wav_sample_rate(data: bytes) -> int:
    """Read the sample rate from a WAV container; 0 if unreadable."""
    if not data:
        return 0
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getframerate()
    except (wave.Error, EOFError):
        return 0


class SpeechSynthesisProvider:
    """Structural interface for TTS backends (mirrors TranscriptionProvider).

    Subclasses implement
    ``async def synthesize(text, *, voice, language) -> SpeechAudio``.
    """

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None
    ) -> SpeechAudio:  # pragma: no cover
        raise NotImplementedError


class OpenAISpeechProvider(SpeechSynthesisProvider):
    """Cloud TTS via OpenAI's ``/v1/audio/speech`` (WAV output)."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str = "gpt-4o-mini-tts",
        voice: str = "alloy",
        language: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.api_url = (
            api_base
            or os.environ.get("OPENAI_SPEECH_BASE_URL")
            or "https://api.openai.com/v1/audio/speech"
        )
        self.model = model
        self.voice = voice
        self.language = language

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None
    ) -> SpeechAudio:
        if not self.api_key:
            logger.warning("OpenAI API key not configured for TTS")
            return SpeechAudio(b"", 0)
        payload = {
            "model": self.model,
            "voice": voice or self.voice,
            "input": text,
            "response_format": "wav",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                self.api_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.content
        return SpeechAudio(data=data, sample_rate=_wav_sample_rate(data))
