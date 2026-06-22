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


class LocalSupertonicProvider(SpeechSynthesisProvider):
    """In-process TTS via the ``supertonic`` package (ONNX).

    Lazy: importing this module never imports ``supertonic``. The TTS engine is
    built once (singleton, lock-guarded) and the synchronous synth runs in a
    worker thread. The ``supertonic`` package self-downloads its model (~260 MB)
    on first use.
    """

    def __init__(self, voice: str = "F4", language: str | None = None,
                 model_dir: str | None = None):
        self.voice = voice
        self.language = language
        self.model_dir = Path(model_dir) if model_dir else None
        self._tts = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        async with self._lock:
            if self._tts is not None:
                return self._tts
            try:
                from supertonic import TTS
            except ImportError as e:
                raise RuntimeError(
                    "Local TTS needs the [tts] extra: "
                    "pip install durin-agent[tts]"
                ) from e
            self._tts = await asyncio.to_thread(lambda: TTS(auto_download=True))
            return self._tts

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None
    ) -> SpeechAudio:
        tts = await self._ensure()
        return await asyncio.to_thread(self._synth_sync, tts, text, voice or self.voice)

    def _synth_sync(self, tts, text: str, voice: str) -> SpeechAudio:
        style = tts.get_voice_style(voice_name=voice)
        wav, _duration = tts.synthesize(text, voice_style=style)
        # Use the package's own writer to produce a valid WAV, then read the
        # bytes + sample rate back (avoids guessing the array dtype / SR attr).
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            tts.save_audio(wav, tmp_path)
            data = Path(tmp_path).read_bytes()
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return SpeechAudio(data=data, sample_rate=_wav_sample_rate(data))


class FallbackSpeechProvider(SpeechSynthesisProvider):
    """Try the primary backend; on failure or empty audio, use the fallback.

    Net-new: the transcription subsystem has no fallback wrapper, and the LLM
    ``FallbackProvider`` is a different (circuit-breaker) shape. This is a plain
    one-shot failover — adequate for TTS where a turn either renders or doesn't.
    """

    def __init__(self, primary: SpeechSynthesisProvider,
                 fallback: SpeechSynthesisProvider):
        self._primary = primary
        self._fallback = fallback

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None
    ) -> SpeechAudio:
        try:
            out = await self._primary.synthesize(text, voice=voice, language=language)
            if out.data:
                return out
            logger.warning("Primary TTS returned empty audio; falling back")
        except Exception as e:  # noqa: BLE001 — failover is the whole point
            logger.warning("Primary TTS failed ({}); falling back", e)
        # Voice ids are provider-specific; let the fallback use its own default.
        return await self._fallback.synthesize(text, voice=None, language=language)
