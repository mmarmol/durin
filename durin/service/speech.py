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
    """Lazily builds a TTS provider and synthesizes text to WAV audio.

    Lifecycle: ``predownload`` at boot verifies the model files exist without
    keeping the engine resident; the first ``synthesize`` loads it (lazy);
    ``unload_if_idle`` drops it again after a quiet period.
    """

    def __init__(
        self,
        provider_factory: Callable[[], SpeechSynthesisProvider],
        *,
        enabled: bool = True,
        provider_name: str = "local",
    ):
        self._provider_factory = provider_factory
        self._provider: SpeechSynthesisProvider | None = None
        self.enabled = enabled
        self._provider_name = provider_name
        self._last_used: float | None = None

    def _get(self) -> SpeechSynthesisProvider:
        if self._provider is None:
            self._provider = self._provider_factory()
        return self._provider

    async def synthesize(
        self, text: str, *, voice: str | None = None, language: str | None = None
    ) -> SpeechAudio:
        if not self.enabled or not text.strip():
            return SpeechAudio(b"", 0)
        import time

        self._last_used = time.monotonic()
        return await self._get().synthesize(text, voice=voice, language=language)

    async def warmup(self) -> None:
        """Build the provider + pre-load its engine so the first synth at
        use-time is instant. No-op when disabled."""
        if not self.enabled:
            return
        await self._get().warmup()

    async def predownload(self) -> None:
        """Ensure the engine's model files exist WITHOUT leaving it resident.

        First run per install pays the engine build (that is what downloads
        the model), then releases it and records a marker; later boots
        short-circuit on the marker. No-op when disabled."""
        if not self.enabled:
            return
        from durin.service.voice_lifecycle import verified_marker_path

        marker = verified_marker_path("tts", self._provider_name)
        if marker.exists():
            return
        await self._get().warmup()
        self._provider = None   # release the engine; use-time reloads lazily
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")

    def unload_if_idle(self, idle_s: float) -> bool:
        """Drop the engine after ``idle_s`` without use. Returns True when
        something was actually unloaded. ``idle_s <= 0`` disables."""
        if idle_s <= 0 or self._provider is None:
            return False
        import time

        if self._last_used is not None and (
                time.monotonic() - self._last_used) < idle_s:
            return False
        self._provider = None
        return True

    @classmethod
    def from_config(cls, config: Any) -> "SpeechSynthesisService":
        """Build the service from a :class:`TtsConfig`."""

        def factory() -> SpeechSynthesisProvider:
            primary = _build_provider(config.provider, config)
            if config.fallback != "none" and config.fallback != config.provider:
                from durin.providers.speech import FallbackSpeechProvider

                return FallbackSpeechProvider(primary, _build_provider(config.fallback, config))
            return primary

        return cls(provider_factory=factory, enabled=config.enabled,
                   provider_name=config.provider)
