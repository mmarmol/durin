"""Text-to-speech service: builds a provider from TtsConfig and synthesizes.

Mirrors durin/service/transcription.py — provider selection is a string-dispatch
in ``from_config`` (NOT in providers/registry.py, which is LLM-only).
"""

from __future__ import annotations

from typing import Any, Callable

from durin.providers.speech import SpeechAudio, SpeechSynthesisProvider


def _build_provider(name: str, config: Any) -> SpeechSynthesisProvider:
    if name == "local":
        from durin.providers.speech import LocalSupertonicProvider

        return LocalSupertonicProvider(
            voice=config.local.voice,
            language=config.language,
            model_dir=config.local.model_dir,
        )
    if name == "openai":
        from durin.providers.speech import OpenAISpeechProvider

        return OpenAISpeechProvider(
            api_key=config.openai.api_key,
            api_base=config.openai.api_base,
            language=config.language,
        )
    raise ValueError(f"Unknown TTS provider: {name}")


class SpeechSynthesisService:
    """Lazily builds a TTS provider and synthesizes text to WAV audio."""

    def __init__(
        self,
        provider_factory: Callable[[], SpeechSynthesisProvider],
        *,
        enabled: bool = True,
    ):
        self._provider_factory = provider_factory
        self._provider: SpeechSynthesisProvider | None = None
        self.enabled = enabled

    def _get(self) -> SpeechSynthesisProvider:
        if self._provider is None:
            self._provider = self._provider_factory()
        return self._provider

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None
    ) -> SpeechAudio:
        if not self.enabled or not text.strip():
            return SpeechAudio(b"", 0)
        return await self._get().synthesize(text, voice=voice, language=language)

    @classmethod
    def from_config(cls, config: Any) -> "SpeechSynthesisService":
        """Build the service from a :class:`TtsConfig`."""

        def factory() -> SpeechSynthesisProvider:
            primary = _build_provider(config.provider, config)
            if config.fallback != "none" and config.fallback != config.provider:
                from durin.providers.speech import FallbackSpeechProvider

                return FallbackSpeechProvider(primary, _build_provider(config.fallback, config))
            return primary

        return cls(provider_factory=factory, enabled=config.enabled)
