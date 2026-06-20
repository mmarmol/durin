"""Voice transcription providers (Groq, OpenAI Whisper, and local sherpa-onnx)."""

import asyncio
import os
from pathlib import Path

import httpx
from loguru import logger

from durin.providers.audio_decode import decode_to_mono_16k
from durin.providers.stt_models import ENGINES, ensure_model

# Up to 3 retries (4 attempts total) with exponential backoff on transient
# failures. Whisper endpoints occasionally return 502/503 under load, and
# mobile-network transcription callers hit sporadic connect/read errors.
# Without this, a voice message silently becomes the empty string.
_MAX_RETRIES = 3
_BACKOFF_S = (1.0, 2.0, 4.0)
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


async def _post_transcription_with_retry(
    url: str,
    *,
    api_key: str | None,
    path: Path,
    model: str,
    provider_label: str,
    language: str | None = None,
) -> str:
    """POST an audio file for transcription, retrying on transient errors.

    Retries on connect/read/timeout failures and on 408/429/5xx responses.
    Other errors (including 4xx such as 401/403) return "" immediately — the
    caller's config is wrong and retrying only wastes quota.

    When ``language`` is provided, it is forwarded as the ``language``
    multipart field on every attempt (the dict is rebuilt per attempt so the
    same field is present on retries).
    """
    try:
        data = path.read_bytes()
    except OSError as e:
        logger.exception("{} transcription error: cannot read audio file: {}", provider_label, e)
        return ""
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient() as client:
        for attempt in range(_MAX_RETRIES + 1):
            files = {
                "file": (path.name, data),
                "model": (None, model),
            }
            if language:
                files["language"] = (None, language)
            try:
                response = await client.post(url, headers=headers, files=files, timeout=60.0)
            except _RETRYABLE_EXCEPTIONS as e:
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "{} transcription transient error (attempt {}/{}): {}",
                        provider_label,
                        attempt + 1,
                        _MAX_RETRIES + 1,
                        e,
                    )
                    await asyncio.sleep(_BACKOFF_S[attempt])
                    continue
                logger.exception(
                    "{} transcription error after {} attempts: {}",
                    provider_label,
                    _MAX_RETRIES + 1,
                    e,
                )
                return ""
            except Exception as e:
                logger.exception("{} transcription error: {}", provider_label, e)
                return ""

            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                logger.warning(
                    "{} transcription transient HTTP {} (attempt {}/{})",
                    provider_label,
                    response.status_code,
                    attempt + 1,
                    _MAX_RETRIES + 1,
                )
                await asyncio.sleep(_BACKOFF_S[attempt])
                continue

            try:
                response.raise_for_status()
            except Exception as e:
                logger.exception("{} transcription error: {}", provider_label, e)
                return ""

            try:
                payload = response.json()
            except Exception as e:
                logger.exception(
                    "{} transcription error: malformed response body: {}",
                    provider_label,
                    e,
                )
                return ""
            if not isinstance(payload, dict):
                logger.error(
                    "{} transcription error: unexpected response shape: {!r}",
                    provider_label,
                    type(payload).__name__,
                )
                return ""
            return payload.get("text", "")


class OpenAITranscriptionProvider:
    """Voice transcription provider using OpenAI's Whisper API."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        language: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.api_url = (
            api_base
            or os.environ.get("OPENAI_TRANSCRIPTION_BASE_URL")
            or "https://api.openai.com/v1/audio/transcriptions"
        )
        self.language = language or None

    async def transcribe(self, file_path: str | Path) -> str:
        if not self.api_key:
            logger.warning("OpenAI API key not configured for transcription")
            return ""
        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        return await _post_transcription_with_retry(
            self.api_url,
            api_key=self.api_key,
            path=path,
            model="whisper-1",
            provider_label="OpenAI",
            language=self.language,
        )


class GroqTranscriptionProvider:
    """
    Voice transcription provider using Groq's Whisper API.

    Groq offers extremely fast transcription with a generous free tier.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        language: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.api_url = (
            api_base
            or os.environ.get("GROQ_BASE_URL")
            or "https://api.groq.com/openai/v1/audio/transcriptions"
        )
        self.language = language or None

    async def transcribe(self, file_path: str | Path) -> str:
        """
        Transcribe an audio file using Groq.

        Args:
            file_path: Path to the audio file.

        Returns:
            Transcribed text.
        """
        if not self.api_key:
            logger.warning("Groq API key not configured for transcription")
            return ""

        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""

        return await _post_transcription_with_retry(
            self.api_url,
            api_key=self.api_key,
            path=path,
            model="whisper-large-v3",
            provider_label="Groq",
            language=self.language,
        )


class TranscriptionProvider:
    """Structural interface for transcription backends (Protocol-style).

    Existing providers (OpenAI/Groq/LocalSttProvider) conform via duck typing —
    this base exists for ``isinstance`` checks and documentation. Subclasses
    implement ``async def transcribe(file_path) -> str``.
    """

    async def transcribe(self, file_path: str | Path) -> str:  # pragma: no cover
        raise NotImplementedError


def _default_stt_cache() -> Path:
    from durin.config.home import durin_home  # existing helper for ~/.durin
    return durin_home() / "models" / "stt"


class LocalSttProvider(TranscriptionProvider):
    """In-process ASR via sherpa-onnx. Engine must be one of {parakeet, sensevoice}.

    Lazy: importing this module never imports sherpa_onnx. The model is
    downloaded on first use and the recognizer built once (singleton). The
    synchronous decode runs in a worker thread.
    """

    def __init__(self, engine="parakeet", model_dir=None, num_threads=None,
                 language=None, cache_dir=None, on_status=None):
        if engine not in ENGINES:
            raise ValueError(f"Unknown STT engine: {engine!r}")
        self.engine = engine
        # model_dir, when set, must point directly to the directory containing
        # the engine's model files (e.g. encoder.int8.onnx, tokens.txt) — the
        # extracted tarball's inner directory, not its parent.
        self.model_dir = Path(model_dir) if model_dir else None
        self.num_threads = num_threads or 2
        self.language = language or ""
        self.cache_dir = Path(cache_dir) if cache_dir else _default_stt_cache()
        self.on_status = on_status
        self._rec = None
        self._lock = asyncio.Lock()

    def _emit(self, phase, done=0, total=0):
        if self.on_status:
            try:
                self.on_status(phase, done, total)
            except Exception:
                logger.debug("on_status callback raised; ignoring")

    def _load(self):
        if self._rec is not None:
            return self._rec
        try:
            import sherpa_onnx
        except ImportError as e:
            raise RuntimeError(
                "sherpa-onnx is not installed. Install the [stt] extra: "
                "pip install durin-agent[stt]"
            ) from e
        if self.model_dir is not None:
            files = {k: self.model_dir / fname
                     for k, fname in ENGINES[self.engine].files.items()}
        else:
            files = ensure_model(
                self.engine, self.cache_dir,
                on_status=lambda _phase, d, t: self._emit("downloading", d, t),
            )
        self._emit("loading")
        if self.engine == "parakeet":
            self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(files["encoder"]),
                decoder=str(files["decoder"]),
                joiner=str(files["joiner"]),
                tokens=str(files["tokens"]),
                num_threads=self.num_threads,
                model_type="nemo_transducer",
            )
        else:  # sensevoice
            self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(files["model"]),
                tokens=str(files["tokens"]),
                num_threads=self.num_threads,
                language=self.language,
                use_itn=True,
            )
        return self._rec

    @staticmethod
    def _decode_sync(rec, samples, sample_rate):
        stream = rec.create_stream()
        stream.accept_waveform(sample_rate, samples)
        rec.decode_stream(stream)
        return (stream.result.text or "").strip()

    async def _ensure_loaded(self):
        if self._rec is not None:
            return self._rec
        async with self._lock:
            if self._rec is None:
                await asyncio.to_thread(self._load)
        return self._rec

    async def transcribe(self, file_path) -> str:
        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        try:
            rec = await self._ensure_loaded()
        except Exception as e:
            logger.exception("LocalSttProvider load error: {}", e)
            return ""
        try:
            samples, sr = decode_to_mono_16k(path)
            if samples.size == 0:
                return ""
            self._emit("transcribing")
            return await asyncio.to_thread(self._decode_sync, rec, samples, sr)
        except Exception as e:
            logger.exception("LocalSttProvider transcribe error: {}", e)
            return ""
