"""TranscriptionService — backend transcription orchestration (spec §4).

Resolves a :class:`~durin.providers.transcription.TranscriptionProvider`
from config, enforces the configured mode (auto/preview/off), and caches
transcripts next to the audio file so re-attaching the same audio does not
re-pay the transcription cost.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from loguru import logger


@dataclass
class TranscriptResult:
    text: str
    cached: bool
    meta_path: Path | None
    audio_path: Path


ProviderFactory = Callable[[], Any]


class TranscriptionService:
    """Owns provider resolution, caching, and mode gating.

    The ``provider_factory`` callable lets tests inject a fake; in production
    it is built from :class:`~durin.config.schema.TranscriptionConfig` via
    :meth:`from_config`.
    """

    def __init__(
        self,
        *,
        provider_factory: ProviderFactory,
        mode: str = "auto",
        enabled: bool = True,
        cache_transcripts: bool = True,
        model_name: str = "unknown",
    ):
        self._factory = provider_factory
        self.mode = mode
        self.enabled = enabled
        self.cache_transcripts = cache_transcripts
        self.model_name = model_name
        self._provider: Any = None  # lazily constructed

    @classmethod
    def from_config(cls, config: Any) -> "TranscriptionService":
        """Build the service from a :class:`TranscriptionConfig` (spec §4.4)."""
        mode = config.mode
        enabled = config.enabled

        def factory() -> Any:
            if config.provider == "local":
                from durin.providers.transcription import LocalSttProvider

                return LocalSttProvider(
                    engine=config.local.engine,
                    model_dir=config.local.model_dir,
                    num_threads=config.local.num_threads,
                    language=config.language,
                )
            if config.provider == "groq":
                from durin.providers.transcription import GroqTranscriptionProvider

                return GroqTranscriptionProvider(
                    api_key=config.groq.api_key,
                    api_base=config.groq.api_base,
                    language=config.language,
                )
            if config.provider == "openai":
                from durin.providers.transcription import OpenAITranscriptionProvider

                return OpenAITranscriptionProvider(
                    api_key=config.openai.api_key,
                    api_base=config.openai.api_base,
                    language=config.language,
                )
            if config.provider == "http":
                # Reuse the OpenAI client against any OpenAI-compat server.
                from durin.providers.transcription import OpenAITranscriptionProvider

                return OpenAITranscriptionProvider(
                    api_key=config.http.api_key,
                    api_base=config.http.base_url,
                    language=config.language,
                )
            raise ValueError(f"Unknown transcription provider: {config.provider}")

        model_name = _resolve_model_name(config)
        return cls(
            provider_factory=factory,
            mode=mode,
            enabled=enabled,
            cache_transcripts=config.cache_transcripts,
            model_name=model_name,
        )

    def _get_provider(self) -> Any:
        if self._provider is None:
            self._provider = self._factory()
        return self._provider

    async def transcribe_and_cache(
        self, file_path: str | Path, on_status: Callable | None = None
    ) -> TranscriptResult:
        path = Path(file_path)
        if not self.enabled or self.mode == "off":
            return TranscriptResult(
                text="", cached=False, meta_path=None, audio_path=path
            )

        txt_path = path.with_suffix(path.suffix + ".txt")
        meta_path = path.with_suffix(path.suffix + ".meta.json")

        # Cache hit only when the stored transcript matches the current model.
        if self.cache_transcripts and txt_path.exists() and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                if meta.get("model") == self.model_name:
                    return TranscriptResult(
                        text=txt_path.read_text(),
                        cached=True,
                        meta_path=meta_path,
                        audio_path=path,
                    )
                logger.debug(
                    "Transcript cache stale (model {} -> {}); retranscribing",
                    meta.get("model"),
                    self.model_name,
                )
            except (OSError, json.JSONDecodeError):
                pass  # fall through to retranscribe

        provider = self._get_provider()
        if on_status is not None and hasattr(provider, "on_status"):
            provider.on_status = on_status
        try:
            text = await provider.transcribe(path)
        except Exception:
            logger.exception("Transcription failed for {}", path)
            text = ""

        if self.cache_transcripts and text:
            try:
                txt_path.write_text(text)
                meta_path.write_text(
                    json.dumps(
                        {"model": self.model_name, "transcribed_at": time.time()}
                    )
                )
            except OSError:
                logger.warning(
                    "Could not write transcript cache for {}", path
                )

        return TranscriptResult(
            text=text,
            cached=False,
            meta_path=meta_path if self.cache_transcripts else None,
            audio_path=path,
        )


def _resolve_model_name(config: Any) -> str:
    if config.provider == "local":
        return config.local.engine
    if config.provider == "http":
        return config.http.model or "http"
    return config.provider
