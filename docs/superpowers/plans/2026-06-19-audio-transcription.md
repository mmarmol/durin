> **Superseded (2026-06-20):** the local STT engine was changed from Whisper (faster-whisper) to sherpa-onnx (Parakeet TDT v3 default + SenseVoice for CJK).

# Audio Transcription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class audio input (attach + record) across webui, TUI, and channels, transcribing to text in the backend so the LLM never sees raw audio.

**Architecture:** A new `TranscriptionService` (backend) resolves a `TranscriptionProvider` from config. The default provider is `LocalWhisperProvider` (in-process faster-whisper); HTTP passthrough reuses the existing `OpenAITranscriptionProvider`. Frontends upload raw audio; the service transcribes and caches the transcript next to the audio file; the agent loop receives only text.

**Tech Stack:** Python 3.11+, faster-whisper (CTranslate2), pydantic (config schema), pytest (backend), React + TypeScript + vitest (webui), Textual (TUI), sounddevice (TUI mic), MediaRecorder API (webui mic).

**Spec:** `docs/superpowers/specs/2026-06-19-audio-transcription-design.md`

**Worktree:** This plan executes in the sibling worktree `/Users/marcelo/git_personal/durin-audio`, branch `feat/audio-input`. Task 0 creates it.

---

## File Structure

### Backend (Python)

| File | Responsibility | Status |
|---|---|---|
| `pyproject.toml` | Add `[stt]` and `[voice]` extras. | Modify |
| `durin/config/schema.py` | Add `TranscriptionConfig` + wire into root config. | Modify |
| `durin/providers/transcription.py` | Add `TranscriptionProvider` Protocol + `LocalWhisperProvider`. | Modify |
| `durin/service/transcription.py` | New `TranscriptionService`: provider resolution, caching, modes. | Create |
| `durin/channels/base.py` | Delegate `transcribe_audio` to `TranscriptionService`. | Modify |
| `durin/channels/websocket.py` | Accept audio MIME in uploads; add `audio_transcribe` envelope handler. | Modify |
| `durin/cli/dragdrop.py` | (No change — already copies audio; wiring happens in TUI entrypoint.) | — |
| `durin/cli/tui/widgets/composer.py` | Route dragged audio through the service; add `/voice` recorder. | Modify |
| `durin/cli/doctor.py` | Add `stt.*` checks. | Modify |
| `docs/INSTALL.md` | Document `[stt]`/`[voice]` extras and PortAudio. | Modify |

### Frontend (webui)

| File | Responsibility | Status |
|---|---|---|
| `webui/src/hooks/useAttachedAudio.ts` | Audio attachment lifecycle hook (mirrors `useAttachedImages`). | Create |
| `webui/src/components/thread/MicButton.tsx` | Microphone recorder component. | Create |
| `webui/src/components/thread/AudioChip.tsx` | Audio chip with player + status. | Create |
| `webui/src/components/thread/ThreadComposer.tsx` | Wire audio hook + MicButton + WS transcript handler. | Modify |
| `webui/src/lib/audioMime.ts` | `pickAudioMime()` for MediaRecorder. | Create |

### Tests

| File | Covers |
|---|---|
| `tests/providers/test_transcription.py` | `LocalWhisperProvider`, provider resolution, cache, duration cap. |
| `tests/service/test_transcription_service.py` | Service modes, idempotency, error handling. |
| `tests/channels/test_websocket_audio.py` | Audio MIME acceptance + `audio_transcribe` round-trip. |
| `tests/cli/test_doctor_stt.py` | Doctor STT checks. |
| `webui/src/hooks/useAttachedAudio.test.ts` | MIME/cap/lifecycle. |
| `webui/src/components/thread/MicButton.test.tsx` | Recording + permission denied. |

---

## Task 0: Create the worktree

**Files:** none (git plumbing)

- [ ] **Step 1: Create the sibling worktree on a new branch**

```bash
git worktree add /Users/marcelo/git_personal/durin-audio -b feat/audio-input
```

- [ ] **Step 2: Verify it**

Run: `cd /Users/marcelo/git_personal/durin-audio && git branch --show-current`
Expected: `feat/audio-input`

All subsequent tasks run inside `/Users/marcelo/git_personal/durin-audio`.

---

## Task 1: Add the `[stt]` and `[voice]` extras

**Files:**
- Modify: `pyproject.toml:59` (the `[project.optional-dependencies]` table)

- [ ] **Step 1: Add the two extras after the `local` extra**

Insert after the `local = [...]` block (ends at line 80):

```toml
stt = [
    # In-process Whisper via CTranslate2. Prebuilt wheels for x64/arm64 on
    # Linux, macOS, Windows (no compilation). Model weights download on first
    # use (~1.5GB for large-v3) to the HuggingFace cache.
    "faster-whisper>=1.1.0,<2.0.0",
]
voice = [
    # Cross-platform microphone capture for the TUI /voice command.
    # Bundles PortAudio on macOS/Windows; Linux needs libportaudio2.
    "sounddevice>=0.5.0,<1.0.0",
]
```

- [ ] **Step 2: Sync the env**

Run: `uv sync --extra stt --extra voice`
Expected: faster-whisper and sounddevice install cleanly.

- [ ] **Step 3: Verify imports**

Run: `python -c "import faster_whisper, sounddevice; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build(pyproject): add [stt] and [voice] optional extras"
```

---

## Task 2: Add the `TranscriptionConfig` schema

**Files:**
- Modify: `durin/config/schema.py` (add classes before the root config)
- Test: `tests/config/test_schema.py` (create if missing)

- [ ] **Step 1: Write the failing test**

Create `tests/config/test_transcription_schema.py`:

```python
from durin.config.schema import TranscriptionConfig, RootConfig


def test_transcription_defaults():
    cfg = TranscriptionConfig()
    assert cfg.enabled is True
    assert cfg.mode == "auto"
    assert cfg.provider == "local"
    assert cfg.language is None
    assert cfg.local.model == "large-v3"
    assert cfg.local.device == "auto"
    assert cfg.local.compute_type == "auto"
    assert cfg.max_duration_s == 600
    assert cfg.cache_transcripts is True


def test_transcription_mode_invalid():
    import pytest
    with pytest.raises(Exception):
        TranscriptionConfig(mode="bogus")


def test_transcription_language_pattern():
    import pytest
    with pytest.raises(Exception):
        TranscriptionConfig(language="spanish")  # must be ISO-639-1


def test_root_config_has_transcription():
    root = RootConfig()
    assert isinstance(root.transcription, TranscriptionConfig)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/config/test_transcription_schema.py -v`
Expected: FAIL — `TranscriptionConfig` not defined / `RootConfig.transcription` missing.

- [ ] **Step 3: Add the schema classes**

In `durin/config/schema.py`, add after `AuxModelConfig` (or before the root config class):

```python
class TranscriptionLocalConfig(Base):
    """Local faster-whisper settings."""
    model: Literal["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"] = "large-v3"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: Literal["auto", "int8", "int8_float16", "float16", "float32"] = "auto"
    download_root: str | None = None


class TranscriptionHttpConfig(Base):
    """OpenAI-compatible HTTP server endpoint (whisper.cpp, mlx-qwen3-asr, vLLM)."""
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class TranscriptionProviderKeysConfig(Base):
    """Cloud API credentials for a named provider."""
    api_key: str | None = None
    api_base: str | None = None


class TranscriptionConfig(Base):
    """Global transcription settings (spec §4.3).

    Channel-level ``transcription_provider`` / ``transcription_api_key`` /
    ``transcription_language`` override these per-channel.
    """
    enabled: bool = True
    mode: Literal["auto", "preview", "off"] = "auto"
    provider: Literal["local", "openai", "groq", "http"] = "local"
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")
    local: TranscriptionLocalConfig = TranscriptionLocalConfig()
    http: TranscriptionHttpConfig = TranscriptionHttpConfig()
    openai: TranscriptionProviderKeysConfig = TranscriptionProviderKeysConfig()
    groq: TranscriptionProviderKeysConfig = TranscriptionProviderKeysConfig()
    max_duration_s: int = Field(default=600, ge=1, le=86400)
    cache_transcripts: bool = True
```

Then add the field to the root config class (find the root config class — typically `RootConfig` or `DurinConfig` — and add):

```python
    transcription: TranscriptionConfig = TranscriptionConfig()
```

> **Note:** The exact root class name must be confirmed by reading `schema.py`. Look for the class that aggregates `ChannelsConfig`, `agents`, etc. Add `transcription` alongside those.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/config/test_transcription_schema.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add durin/config/schema.py tests/config/test_transcription_schema.py
git commit -m "feat(config): add TranscriptionConfig schema"
```

---

## Task 3: Add the `TranscriptionProvider` Protocol + `LocalWhisperProvider`

**Files:**
- Modify: `durin/providers/transcription.py` (append to existing file)
- Test: `tests/providers/test_local_whisper.py`

- [ ] **Step 1: Write the failing test**

Create `tests/providers/test_local_whisper.py`:

```python
import pytest

from durin.providers.transcription import LocalWhisperProvider, TranscriptionProvider


def test_local_provider_is_transcription_provider():
    # Structural check: LocalWhisperProvider must satisfy the Protocol.
    p = LocalWhisperProvider(model="tiny", device="cpu", compute_type="int8")
    assert isinstance(p, TranscriptionProvider)


def test_local_provider_constructs_without_faster_whisper_installed(monkeypatch):
    # Importing must not require faster-whisper; only transcribe() does.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("simulated absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Construction must succeed even if faster_whisper is unimportable.
    p = LocalWhisperProvider(model="tiny", device="cpu", compute_type="int8")
    assert p.model == "tiny"


@pytest.mark.asyncio
async def test_local_provider_transcribe_calls_model(monkeypatch, tmp_path):
    """transcribe() lazily imports faster_whisper, runs in a thread, returns text."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    captured = {}

    class FakeSegments:
        def __iter__(self):
            yield {"text": "  hello world  "}, None

    class FakeModel:
        def __init__(self, *a, **kw):
            captured["init_args"] = (a, kw)

        def transcribe(self, path, **kw):
            captured["transcribe_args"] = kw
            return FakeSegments(), None

    class FakeWhisperModule:
        WhisperModel = FakeModel

    import sys
    sys.modules["faster_whisper"] = FakeWhisperModule

    p = LocalWhisperProvider(
        model="tiny", device="cpu", compute_type="int8", language="en",
    )
    text = await p.transcribe(audio)
    assert text == "hello world"
    assert captured["transcribe_args"].get("language") == "en"


@pytest.mark.asyncio
async def test_local_provider_transcribe_missing_file(tmp_path):
    p = LocalWhisperProvider(model="tiny", device="cpu", compute_type="int8")
    text = await p.transcribe(tmp_path / "nope.wav")
    assert text == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/providers/test_local_whisper.py -v`
Expected: FAIL — `LocalWhisperProvider` / `TranscriptionProvider` not defined.

- [ ] **Step 3: Append the Protocol + provider to `durin/providers/transcription.py`**

Add at the end of the file:

```python
from __future__ import annotations  # keep at top of file instead if not present

from typing import Protocol


class TranscriptionProvider(Protocol):
    """Structural interface for transcription backends.

    Existing providers (OpenAI/Groq) already conform: ``async def
    transcribe(file_path) -> str``. ``LocalWhisperProvider`` conforms too.
    """

    async def transcribe(self, file_path: str | Path) -> str: ...


class LocalWhisperProvider:
    """In-process Whisper via faster-whisper (CTranslate2).

    Construction does NOT import faster-whisper — that happens lazily on the
    first ``transcribe()`` so a missing ``[stt]`` extra does not break module
    import. The synchronous model runs in a worker thread via
    ``asyncio.to_thread`` to avoid blocking the event loop.
    """

    def __init__(
        self,
        model: str = "large-v3",
        device: str = "auto",
        compute_type: str = "auto",
        language: str | None = None,
        download_root: str | None = None,
    ):
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.language = language or None
        self.download_root = download_root or None
        self._model_obj = None  # lazily loaded singleton

    def _load(self):
        if self._model_obj is not None:
            return self._model_obj
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:  # pragma: no cover - exercised via doctor
            raise RuntimeError(
                "faster-whisper is not installed. Install the [stt] extra: "
                "pip install durin-agent[stt]"
            ) from e
        self._model_obj = WhisperModel(
            self.model,
            device=self.device,
            compute_type=self.compute_type,
            download_root=self.download_root,
        )
        return self._model_obj

    @staticmethod
    def _transcribe_sync(model, path, language):
        segments, _info = model.transcribe(str(path), language=language)
        return " ".join(seg.text for seg in segments).strip()

    async def transcribe(self, file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        try:
            model = self._load()
        except Exception as e:
            logger.exception("LocalWhisperProvider load error: {}", e)
            return ""
        try:
            return await asyncio.to_thread(
                self._transcribe_sync, model, path, self.language
            )
        except Exception as e:
            logger.exception("LocalWhisperProvider transcribe error: {}", e)
            return ""
```

> **Note:** `logger`, `asyncio`, and `Path` are already imported at the top of the file (verified during planning). If `Path` is imported only for type hints, ensure `from pathlib import Path` is present.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/providers/test_local_whisper.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add durin/providers/transcription.py tests/providers/test_local_whisper.py
git commit -m "feat(providers): add TranscriptionProvider protocol + LocalWhisperProvider"
```

---

## Task 4: Add the `TranscriptionService`

**Files:**
- Create: `durin/service/transcription.py`
- Test: `tests/service/test_transcription_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/service/test_transcription_service.py`:

```python
import json
from pathlib import Path

import pytest

from durin.service.transcription import (
    TranscriptResult,
    TranscriptionService,
)


class FakeProvider:
    def __init__(self, text="hello", fail=False):
        self.text = text
        self.fail = fail
        self.calls = 0

    async def transcribe(self, file_path):
        self.calls += 1
        if self.fail:
            return ""
        return self.text


def _make_wav(path: Path):
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")


@pytest.mark.asyncio
async def test_transcribe_returns_text(tmp_path):
    audio = tmp_path / "a.wav"
    _make_wav(audio)
    svc = TranscriptionService(provider_factory=lambda: FakeProvider("hi"))
    result = await svc.transcribe_and_cache(audio)
    assert result.text == "hi"
    assert result.cached is False


@pytest.mark.asyncio
async def test_transcribe_caches(tmp_path):
    audio = tmp_path / "a.wav"
    _make_wav(audio)
    fake = FakeProvider("hi")
    svc = TranscriptionService(provider_factory=lambda: fake)
    await svc.transcribe_and_cache(audio)
    result = await svc.transcribe_and_cache(audio)
    assert result.cached is True
    assert fake.calls == 1  # second call served from cache


@pytest.mark.asyncio
async def test_transcribe_cache_invalidation_on_model_change(tmp_path):
    audio = tmp_path / "a.wav"
    _make_wav(audio)
    # First transcription with model "A".
    svc_a = TranscriptionService(provider_factory=lambda: FakeProvider("v1"), model_name="A")
    await svc_a.transcribe_and_cache(audio)
    # Same file, different model -> must retranscribe (cache stale).
    fake_b = FakeProvider("v2")
    svc_b = TranscriptionService(provider_factory=lambda: fake_b, model_name="B")
    result = await svc_b.transcribe_and_cache(audio)
    assert result.text == "v2"
    assert result.cached is False


@pytest.mark.asyncio
async def test_transcribe_off_mode_returns_empty(tmp_path):
    audio = tmp_path / "a.wav"
    _make_wav(audio)
    fake = FakeProvider("should not run")
    svc = TranscriptionService(provider_factory=lambda: fake, mode="off")
    result = await svc.transcribe_and_cache(audio)
    assert result.text == ""
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_transcribe_disabled_returns_empty(tmp_path):
    audio = tmp_path / "a.wav"
    _make_wav(audio)
    fake = FakeProvider("should not run")
    svc = TranscriptionService(provider_factory=lambda: fake, enabled=False)
    result = await svc.transcribe_and_cache(audio)
    assert result.text == ""
    assert fake.calls == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/service/test_transcription_service.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Create the service**

Create `durin/service/transcription.py`:

```python
"""TranscriptionService — backend transcription orchestration.

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
from typing import Callable

from loguru import logger

from durin.utils.media_decode import get_media_dir  # adjust import to actual


@dataclass
class TranscriptResult:
    text: str
    cached: bool
    meta_path: Path | None
    audio_path: Path


ProviderFactory = Callable[[], "object"]


class TranscriptionService:
    """Owns provider resolution, caching, and mode gating.

    The ``provider_factory`` callable lets tests inject a fake; in production
    it is built from ``TranscriptionConfig`` (see ``from_config``).
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
        self._provider = None  # lazily constructed

    @classmethod
    def from_config(cls, config) -> "TranscriptionService":
        """Build the service from a :class:`TranscriptionConfig`.

        Provider resolution follows spec §4.4.
        """
        mode = config.mode
        enabled = config.enabled

        def factory():
            if config.provider == "local":
                from durin.providers.transcription import LocalWhisperProvider
                return LocalWhisperProvider(
                    model=config.local.model,
                    device=config.local.device,
                    compute_type=config.local.compute_type,
                    language=config.language,
                    download_root=config.local.download_root,
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

    def _get_provider(self):
        if self._provider is None:
            self._provider = self._factory()
        return self._provider

    async def transcribe_and_cache(self, file_path: str | Path) -> TranscriptResult:
        path = Path(file_path)
        if not self.enabled or self.mode == "off":
            return TranscriptResult(text="", cached=False, meta_path=None, audio_path=path)

        txt_path = path.with_suffix(path.suffix + ".txt")
        meta_path = path.with_suffix(path.suffix + ".meta.json")

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
                logger.debug("Transcript cache stale (model {} -> {}); retranscribing",
                             meta.get("model"), self.model_name)
            except (OSError, json.JSONDecodeError):
                pass  # fall through to retranscribe

        provider = self._get_provider()
        try:
            text = await provider.transcribe(path)
        except Exception:
            logger.exception("Transcription failed for {}", path)
            text = ""

        if self.cache_transcripts and text:
            try:
                txt_path.write_text(text)
                meta_path.write_text(json.dumps({
                    "model": self.model_name,
                    "transcribed_at": time.time(),
                }))
            except OSError:
                logger.warning("Could not write transcript cache for {}", path)

        return TranscriptResult(
            text=text, cached=False, meta_path=meta_path if self.cache_transcripts else None,
            audio_path=path,
        )


def _resolve_model_name(config) -> str:
    if config.provider == "local":
        return config.local.model
    if config.provider == "http":
        return config.http.model or "http"
    return config.provider
```

> **Note:** The `get_media_dir` import may not live in `durin.utils.media_decode` — confirm the actual module (used in `durin/channels/websocket.py`). If the service does not need it (the caller passes the path), drop the import. The test does not exercise media_dir resolution, so remove it if unused.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/service/test_transcription_service.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add durin/service/transcription.py tests/service/test_transcription_service.py
git commit -m "feat(service): add TranscriptionService with cache + modes"
```

---

## Task 5: Refactor `BaseChannel` to delegate to `TranscriptionService`

**Files:**
- Modify: `durin/channels/base.py:52-74`
- Test: `tests/channels/test_base_transcribe.py`

- [ ] **Step 1: Write the failing test**

Create `tests/channels/test_base_transcribe.py`:

```python
from pathlib import Path

import pytest

from durin.service.transcription import TranscriptResult


class FakeService:
    def __init__(self, text="hi from service"):
        self.text = text
        self.calls = 0

    async def transcribe_and_cache(self, path):
        self.calls += 1
        return TranscriptResult(text=self.text, cached=False, meta_path=None, audio_path=Path(path))


@pytest.mark.asyncio
async def test_channel_delegates_to_service(tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    fake = FakeService()

    from durin.channels.base import BaseChannel
    # Concrete subclass for instantiation.
    class Ch(BaseChannel):
        name = "test"
        async def start(self): ...
        async def stop(self): ...

    ch = Ch(config=None, bus=None)
    ch.transcription = fake  # injected
    text = await ch.transcribe_audio(audio)
    assert text == "hi from service"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_channel_falls_back_when_no_service(tmp_path):
    """Legacy path: if no service injected, return '' (no crash)."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    from durin.channels.base import BaseChannel
    class Ch(BaseChannel):
        name = "test"
        async def start(self): ...
        async def stop(self): ...

    ch = Ch(config=None, bus=None)
    # No service injected.
    text = await ch.transcribe_audio(audio)
    assert text == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/channels/test_base_transcribe.py -v`
Expected: FAIL — `transcription` attribute / delegation not present.

- [ ] **Step 3: Modify `BaseChannel.transcribe_audio`**

In `durin/channels/base.py`, replace the body of `transcribe_audio` (lines 52-74) with delegation:

```python
    async def transcribe_audio(self, file_path: str | Path) -> str:
        """Transcribe an audio file.

        Delegates to the injected ``TranscriptionService`` when present
        (spec §7). Falls back to constructing a Groq/OpenAI provider from the
        channel-level config for backward compatibility (legacy channels that
        have not been wired with a service yet).
        """
        service = getattr(self, "transcription", None)
        if service is not None:
            try:
                result = await service.transcribe_and_cache(file_path)
                return result.text
            except Exception:
                self.logger.exception("Audio transcription failed")
                return ""
        # Legacy fallback: channel-level Groq/OpenAI keys.
        if not self.transcription_api_key:
            return ""
        try:
            if self.transcription_provider == "openai":
                from durin.providers.transcription import OpenAITranscriptionProvider
                provider = OpenAITranscriptionProvider(
                    api_key=self.transcription_api_key,
                    api_base=self.transcription_api_base or None,
                    language=self.transcription_language or None,
                )
            else:
                from durin.providers.transcription import GroqTranscriptionProvider
                provider = GroqTranscriptionProvider(
                    api_key=self.transcription_api_key,
                    api_base=self.transcription_api_base or None,
                    language=self.transcription_language or None,
                )
            return await provider.transcribe(file_path)
        except Exception:
            self.logger.exception("Audio transcription failed")
            return ""
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/channels/test_base_transcribe.py -v`
Expected: 2 passed.

- [ ] **Step 5: Run existing channel tests to confirm no regressions**

Run: `pytest tests/channels/ -v -k "telegram or whatsapp"`
Expected: existing tests still pass (they exercise the legacy fallback path).

- [ ] **Step 6: Commit**

```bash
git add durin/channels/base.py tests/channels/test_base_transcribe.py
git commit -m "refactor(channels): BaseChannel delegates to TranscriptionService"
```

---

## Task 6: Accept audio MIME in the websocket channel

**Files:**
- Modify: `durin/channels/websocket.py:320` (`_MEDIA_ALLOWED_MIMES`) and the upload whitelist (`_UPLOAD_MIME_ALLOWED`)
- Test: `tests/channels/test_websocket_audio.py`

- [ ] **Step 1: Locate the upload whitelist**

Run: `grep -n "_UPLOAD_MIME_ALLOWED\|_IMAGE_MIME_ALLOWED\|_VIDEO_MIME_ALLOWED\|_AUDIO" durin/channels/websocket.py`

Record the line numbers; these are the sets `_save_envelope_media` checks (line 1032).

- [ ] **Step 2: Write the failing test**

Create `tests/channels/test_websocket_audio.py`:

```python
import base64


def _data_url(mime: str, payload: bytes = b"OgGS") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def test_audio_mime_allowed_in_whitelist():
    from durin.channels.websocket import _UPLOAD_MIME_ALLOWED
    for m in ("audio/mpeg", "audio/ogg", "audio/webm", "audio/wav",
              "audio/x-m4a", "audio/aac", "audio/flac"):
        assert m in _UPLOAD_MIME_ALLOWED, f"{m} not accepted"


def test_audio_mime_served_by_media_endpoint():
    from durin.channels.websocket import _MEDIA_ALLOWED_MIMES
    # Audio must be servable back to the browser for playback.
    for m in ("audio/mpeg", "audio/ogg", "audio/webm", "audio/wav"):
        assert m in _MEDIA_ALLOWED_MIMES
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/channels/test_websocket_audio.py -v`
Expected: FAIL — audio MIME not in the whitelists.

- [ ] **Step 4: Add audio MIME to both whitelists**

In `durin/channels/websocket.py`, extend `_MEDIA_ALLOWED_MIMES` (line 320) with:

```python
    "audio/mpeg",
    "audio/ogg",
    "audio/opus",
    "audio/wav",
    "audio/webm",
    "audio/x-m4a",
    "audio/aac",
    "audio/flac",
```

Find `_UPLOAD_MIME_ALLOWED` (the set checked at line 1032) and add the same audio MIME set. If an `_AUDIO_MIME_ALLOWED` set does not exist, create one:

```python
_AUDIO_MIME_ALLOWED: frozenset[str] = frozenset({
    "audio/mpeg", "audio/ogg", "audio/opus", "audio/wav",
    "audio/webm", "audio/x-m4a", "audio/aac", "audio/flac",
})
```

and union it into `_UPLOAD_MIME_ALLOWED`:

```python
_UPLOAD_MIME_ALLOWED: frozenset[str] = _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED | _AUDIO_MIME_ALLOWED
```

Also add a cap constant near `_MAX_VIDEO_BYTES` (line 245):

```python
_MAX_AUDIO_BYTES = 25 * 1024 * 1024
```

and in `_save_envelope_media` extend the size selection (line 1034-1035):

```python
            is_audio = mime in _AUDIO_MIME_ALLOWED
            if is_video:
                max_bytes = _MAX_VIDEO_BYTES
            elif is_audio:
                max_bytes = _MAX_AUDIO_BYTES
            else:
                max_bytes = _MAX_IMAGE_BYTES
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/channels/test_websocket_audio.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add durin/channels/websocket.py tests/channels/test_websocket_audio.py
git commit -m "feat(websocket): accept audio MIME in media uploads"
```

---

## Task 7: Add the `audio_transcribe` WS envelope handler

**Files:**
- Modify: `durin/channels/websocket.py` (new branch in `_dispatch_envelope`, near line 1073)
- Test: `tests/channels/test_websocket_audio.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/channels/test_websocket_audio.py`:

```python
import pytest


class FakeConn:
    def __init__(self):
        self.sent = []
    async def send_json(self, payload):
        self.sent.append(payload)
    @property
    def remote(self):
        return "test"


@pytest.mark.asyncio
async def test_audio_transcribe_envelope_returns_transcript(monkeypatch, tmp_path):
    """An audio_transcribe envelope stores the audio and replies with the transcript."""
    from durin.channels.websocket import WebsocketChannel

    audio_bytes = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8
    data_url = _data_url("audio/wav", audio_bytes)

    class FakeService:
        async def transcribe_and_cache(self, path):
            from durin.service.transcription import TranscriptResult
            return TranscriptResult(text="hello from audio", cached=False,
                                    meta_path=None, audio_path=tmp_path / "x")

    ch = WebsocketChannel.__new__(WebsocketChannel)
    ch.transcription = FakeService()
    ch.logger = __import__("loguru").logger
    # Minimal stubs the handler touches:
    ch._save_envelope_media = lambda media: ([str((tmp_path / "a.wav"))], None)

    conn = FakeConn()
    envelope = {"type": "audio_transcribe", "chat_id": "c1",
                "media": [{"data_url": data_url, "name": "a.wav"}]}
    await ch._dispatch_envelope(conn, "client1", envelope)

    assert any(p.get("type") == "audio_transcript" for p in conn.sent)
    reply = next(p for p in conn.sent if p.get("type") == "audio_transcript")
    assert reply["transcript"] == "hello from audio"
```

> **Note:** The test constructs a partial channel. If `_dispatch_envelope`'s signature requires more setup, adjust the stubs to match — the goal is to assert the reply shape.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/channels/test_websocket_audio.py::test_audio_transcribe_envelope_returns_transcript -v`
Expected: FAIL — handler does not exist / no `audio_transcript` reply.

- [ ] **Step 3: Add the handler branch**

In `durin/channels/websocket.py`, inside `_dispatch_envelope`, add a branch (near the existing `if t == "message":` block around line 1073):

```python
        if t == "audio_transcribe":
            cid = envelope.get("chat_id")
            raw_media = envelope.get("media")
            if not isinstance(raw_media, list) or not raw_media:
                await self._send_event(connection, "error", detail="missing media")
                return
            paths, reason = self._save_envelope_media(raw_media)
            if reason is not None:
                await self._send_event(connection, "error", detail="audio_rejected", reason=reason)
                return
            service = getattr(self, "transcription", None)
            for item, path in zip(raw_media, paths):
                name = item.get("name") or Path(path).name
                if service is None:
                    await self._send_event(
                        connection, "audio_transcript",
                        chat_id=cid, name=name, transcript="", error="disabled",
                    )
                    continue
                try:
                    result = await service.transcribe_and_cache(path)
                    await self._send_event(
                        connection, "audio_transcript",
                        chat_id=cid, name=name, transcript=result.text,
                    )
                except Exception:
                    self.logger.exception("audio_transcribe failed for {}", path)
                    await self._send_event(
                        connection, "audio_transcript",
                        chat_id=cid, name=name, transcript="", error="failed",
                    )
            return
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/channels/test_websocket_audio.py::test_audio_transcribe_envelope_returns_transcript -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add durin/channels/websocket.py tests/channels/test_websocket_audio.py
git commit -m "feat(websocket): handle audio_transcribe envelope -> audio_transcript"
```

---

## Task 8: Wire `TranscriptionService` construction at app startup

**Files:**
- Modify: the app bootstrap that constructs channels (find via `grep -rn "WebsocketChannel(" durin/` and the gateway/startup wiring)

- [ ] **Step 1: Locate where channels are constructed**

Run: `grep -rn "transcription" durin/service/wiring.py durin/service/registry.py durin/channels/__init__.py 2>/dev/null`

- [ ] **Step 2: Construct the service from config and attach**

Where channels are instantiated (or in the service wiring module), add:

```python
from durin.service.transcription import TranscriptionService

transcription_service = TranscriptionService.from_config(config.transcription)
# Attach to each channel:
channel.transcription = transcription_service
```

If channels are built in a factory, attach there. If the websocket channel reads config directly, set `self.transcription = TranscriptionService.from_config(...)` in its `__init__`.

- [ ] **Step 3: Smoke test**

Run: `python -c "from durin.service.transcription import TranscriptionService; from durin.config.schema import TranscriptionConfig; print(TranscriptionService.from_config(TranscriptionConfig()).mode)"`
Expected: `auto`

- [ ] **Step 4: Commit**

```bash
git add <wiring files>
git commit -m "feat(wiring): construct and inject TranscriptionService at startup"
```

---

## Task 9: Add `durin doctor` STT checks

**Files:**
- Modify: `durin/cli/doctor.py`
- Test: `tests/cli/test_doctor_stt.py`

- [ ] **Step 1: Read the existing doctor pattern**

Run: `head -120 durin/cli/doctor.py` to mirror the check-registration style.

- [ ] **Step 2: Write the failing test**

Create `tests/cli/test_doctor_stt.py`:

```python
def test_doctor_reports_stt_installed_when_faster_whisper_present(monkeypatch):
    import sys
    import types
    sys.modules.setdefault("faster_whisper", types.ModuleType("faster_whisper"))
    from durin.cli.doctor import check_stt_installed
    result = check_stt_installed()
    assert result.ok is True


def test_doctor_reports_stt_missing_when_absent(monkeypatch):
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **k):
        if name == "faster_whisper":
            raise ImportError("nope")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    from durin.cli.doctor import check_stt_installed
    result = check_stt_installed()
    assert result.ok is False
    assert "stt" in (result.hint or "").lower() or "faster-whisper" in (result.hint or "").lower()
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/cli/test_doctor_stt.py -v`
Expected: FAIL — `check_stt_installed` not defined.

- [ ] **Step 4: Implement the checks**

In `durin/cli/doctor.py`, add (mirroring the existing check function style — return type is whatever the other checks return, e.g. a dataclass `Check` with `.ok` and `.hint`):

```python
def check_stt_installed() -> "Check":
    try:
        import faster_whisper  # noqa: F401
        return Check(ok=True, name="stt.installed", hint=None)
    except ImportError:
        return Check(
            ok=False,
            name="stt.installed",
            hint="faster-whisper not installed. pip install durin-agent[stt]",
        )
```

Register it in the same list the doctor iterates. Add a second check `check_stt_cloud_keys` that, when `config.transcription.provider in {"groq", "openai"}`, verifies the relevant API key is set.

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/cli/test_doctor_stt.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add durin/cli/doctor.py tests/cli/test_doctor_stt.py
git commit -m "feat(doctor): add stt.installed and stt.cloud_keys checks"
```

---

## Task 10: webui — `audioMime` helper

**Files:**
- Create: `webui/src/lib/audioMime.ts`

- [ ] **Step 1: Create the helper**

```typescript
/** Pick the best audio MIME this browser can record with MediaRecorder.
 *
 * Preference order:
 *   1. audio/webm;codecs=opus  (Chrome, Firefox — smaller, broadly accepted)
 *   2. audio/mp4                 (Safari fallback)
 *   3. "" (default)              (let the browser choose)
 *
 * Returns "" when MediaRecorder is unavailable (caller should hide the mic).
 */
export function pickAudioMime(): string {
  if (typeof MediaRecorder === "undefined") return "";
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
  ];
  for (const c of candidates) {
    if (MediaRecorder.isTypeSupported(c)) return c;
  }
  return "";
}
```

- [ ] **Step 2: Commit**

```bash
git add webui/src/lib/audioMime.ts
git commit -m "feat(webui): add pickAudioMime helper"
```

---

## Task 11: webui — `useAttachedAudio` hook

**Files:**
- Create: `webui/src/hooks/useAttachedAudio.ts`
- Test: `webui/src/hooks/useAttachedAudio.test.ts`

- [ ] **Step 1: Write the failing test**

Create `webui/src/hooks/useAttachedAudio.test.ts`:

```typescript
import { renderHook, act } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useAttachedAudio } from "./useAttachedAudio";

function wavFile(name = "a.wav", size = 1000): File {
  const blob = new Blob([new Uint8Array(size)], { type: "audio/wav" });
  return new File([blob], name, { type: "audio/wav" });
}

describe("useAttachedAudio", () => {
  it("accepts a supported audio file", () => {
    const { result } = renderHook(() => useAttachedAudio());
    act(() => { result.current.enqueue([wavFile()]); });
    expect(result.current.audio).toHaveLength(1);
    expect(result.current.audio[0].status).toBe("ready");
  });

  it("rejects unsupported MIME", () => {
    const { result } = renderHook(() => useAttachedAudio());
    const bad = new File([new Blob(["x"])], "a.txt", { type: "text/plain" });
    let rejected: any;
    act(() => { rejected = result.current.enqueue([bad]); });
    expect(rejected.rejected).toHaveLength(1);
    expect(result.current.audio).toHaveLength(0);
  });

  it("enforces a single attachment cap", () => {
    const { result } = renderHook(() => useAttachedAudio());
    act(() => { result.current.enqueue([wavFile("a.wav")]); });
    let rejected: any;
    act(() => { rejected = result.current.enqueue([wavFile("b.wav")]); });
    expect(rejected.rejected.length).toBe(1);
    expect(result.current.audio).toHaveLength(1);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd webui && bun run test -- useAttachedAudio`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the hook**

Create `webui/src/hooks/useAttachedAudio.ts` (mirror the structure of `useAttachedImages.ts` but simpler — no Worker, since audio is not re-encoded):

```typescript
import { useCallback, useEffect, useRef, useState } from "react";

export type AudioAttachmentStatus = "ready" | "error";

export interface AttachedAudio {
  id: string;
  file: File;
  previewUrl: string;   // blob URL for the <audio> player
  status: AudioAttachmentStatus;
  durationS?: number;
  error?: string;
}

export interface UseAttachedAudioApi {
  audio: AttachedAudio[];
  enqueue: (files: Iterable<File>) => { rejected: Array<{ file: File; reason: string }> };
  remove: (id: string) => void;
  clear: () => void;
  full: boolean;
}

export const MAX_AUDIO_PER_MESSAGE = 1;
export const MAX_AUDIO_BYTES = 25 * 1024 * 1024;

const ACCEPTED_MIMES: ReadonlySet<string> = new Set([
  "audio/mpeg", "audio/ogg", "audio/opus", "audio/wav",
  "audio/webm", "audio/x-m4a", "audio/aac", "audio/flac",
]);

function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `aud-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function useAttachedAudio(): UseAttachedAudioApi {
  const [audio, setAudio] = useState<AttachedAudio[]>([]);
  const audioRef = useRef<AttachedAudio[]>([]);
  audioRef.current = audio;

  const enqueue = useCallback((files: Iterable<File>) => {
    const rejected: Array<{ file: File; reason: string }> = [];
    const toAdd: AttachedAudio[] = [];
    let slot = MAX_AUDIO_PER_MESSAGE - audioRef.current.length;

    for (const file of files) {
      if (!ACCEPTED_MIMES.has(file.type)) {
        rejected.push({ file, reason: "unsupported_type" });
        continue;
      }
      if (file.size > MAX_AUDIO_BYTES) {
        rejected.push({ file, reason: "too_large" });
        continue;
      }
      if (slot <= 0) {
        rejected.push({ file, reason: "too_many" });
        continue;
      }
      slot -= 1;
      toAdd.push({
        id: uuid(),
        file,
        previewUrl: URL.createObjectURL(file),
        status: "ready",
      });
    }
    if (toAdd.length > 0) {
      const next = [...audioRef.current, ...toAdd];
      audioRef.current = next;
      setAudio(next);
    }
    return { rejected };
  }, []);

  const remove = useCallback((id: string) => {
    setAudio((prev) => {
      const target = prev.find((a) => a.id === id);
      if (target) {
        try { URL.revokeObjectURL(target.previewUrl); } catch { /* best-effort */ }
      }
      const next = prev.filter((a) => a.id !== id);
      audioRef.current = next;
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    setAudio((prev) => {
      for (const a of prev) {
        try { URL.revokeObjectURL(a.previewUrl); } catch { /* best-effort */ }
      }
      audioRef.current = [];
      return [];
    });
  }, []);

  useEffect(() => {
    return () => {
      for (const a of audioRef.current) {
        try { URL.revokeObjectURL(a.previewUrl); } catch { /* best-effort */ }
      }
    };
  }, []);

  const full = audio.length >= MAX_AUDIO_PER_MESSAGE;
  return { audio, enqueue, remove, clear, full };
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd webui && bun run test -- useAttachedAudio`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add webui/src/hooks/useAttachedAudio.ts webui/src/hooks/useAttachedAudio.test.ts
git commit -m "feat(webui): add useAttachedAudio hook"
```

---

## Task 12: webui — `<MicButton />` component

**Files:**
- Create: `webui/src/components/thread/MicButton.tsx`
- Test: `webui/src/components/thread/MicButton.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `webui/src/components/thread/MicButton.test.tsx`:

```tsx
import { render, screen, fireEvent, act } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MicButton } from "./MicButton";

describe("MicButton", () => {
  beforeEach(() => {
    // MediaRecorder stub
    (globalThis as any).MediaRecorder = class {
      static isTypeSupported() { return true; }
      state = "inactive";
      onstop: any;
      chunks: BlobPart[] = [];
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.onstop?.(); }
    };
    (navigator as any).mediaDevices = {
      getUserMedia: vi.fn().mockResolvedValue(new MediaStream()),
    };
  });

  it("renders a mic button", () => {
    const onRecorded = vi.fn();
    render(<MicButton onRecorded={onRecorded} />);
    expect(screen.getByRole("button", { name: /mic/i })).toBeTruthy();
  });

  it("disables when MediaRecorder is absent", () => {
    delete (globalThis as any).MediaRecorder;
    render(<MicButton onRecorded={vi.fn()} />);
    const btn = screen.getByRole("button", { name: /mic/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd webui && bun run test -- MicButton`
Expected: FAIL — component not found.

- [ ] **Step 3: Implement the component**

Create `webui/src/components/thread/MicButton.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import { pickAudioMime } from "@/lib/audioMime";

interface MicButtonProps {
  onRecorded: (file: File) => void;
  disabled?: boolean;
}

export function MicButton({ onRecorded, disabled }: MicButtonProps) {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);

  const supported = typeof MediaRecorder !== "undefined";

  async function start() {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const mime = pickAudioMime();
      const rec = mime ? new MediaRecorder(stream, { mimeType: mime })
                       : new MediaRecorder(stream);
      chunksRef.current = [];
      rec.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      rec.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: rec.mimeType || "audio/webm" });
        const ext = (rec.mimeType || "audio/webm").includes("mp4") ? "m4a" : "webm";
        const file = new File([blob], `recording.${ext}`, { type: blob.type });
        onRecorded(file);
        streamRef.current?.getTracks().forEach((t) => t.stop());
      };
      rec.start();
      recorderRef.current = rec;
      setRecording(true);
    } catch {
      setError("Allow microphone access to record.");
    }
  }

  function stop() {
    recorderRef.current?.stop();
    setRecording(false);
  }

  useEffect(() => {
    return () => { streamRef.current?.getTracks().forEach((t) => t.stop()); };
  }, []);

  return (
    <>
      <button
        type="button"
        aria-label="mic"
        disabled={!supported || disabled}
        onClick={recording ? stop : start}
        className={recording ? "animate-pulse bg-red-600" : ""}
      >
        {recording ? "⏹" : "🎙"}
      </button>
      {error && <span role="alert" className="text-red-500 text-xs">{error}</span>}
    </>
  );
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd webui && bun run test -- MicButton`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/thread/MicButton.tsx webui/src/components/thread/MicButton.test.tsx
git commit -m "feat(webui): add MicButton recorder component"
```

---

## Task 13: webui — wire audio into `ThreadComposer`

**Files:**
- Modify: `webui/src/components/thread/ThreadComposer.tsx`

- [ ] **Step 1: Read the current composer**

Run: `sed -n '680,740p' webui/src/components/thread/ThreadComposer.tsx` (the `submit` and attachment area).

- [ ] **Step 2: Integrate**

In `ThreadComposer.tsx`:
1. Import `useAttachedAudio` and `MicButton`.
2. Call `const audio = useAttachedAudio();` next to the existing `useAttachedImages` call.
3. Add `<MicButton onRecorded={(f) => audio.enqueue([f])} />` beside the attach button.
4. Render audio chips (an `<AudioChip>` or inline `<audio controls>`) next to image chips.
5. On submit, for each audio attachment send an `audio_transcribe` envelope and wait for the `audio_transcript` reply, then insert the transcript text into the message `content` before sending.
6. Handle the reply in the existing WS message dispatcher: when `type === "audio_transcript"`, resolve the pending promise.

- [ ] **Step 3: Manual smoke test**

Run `durin gateway start`, open the webui, attach a short wav, and confirm the transcript appears in the input. Record via mic and confirm the same.

- [ ] **Step 4: Commit**

```bash
git add webui/src/components/thread/ThreadComposer.tsx
git commit -m "feat(webui): wire audio attach + mic + transcript into composer"
```

---

## Task 14: TUI — route dragged audio through `TranscriptionService`

**Files:**
- Modify: the TUI entry that calls `process_dragged_paths` (find via `grep -rn "process_dragged_paths" durin/cli/`)

- [ ] **Step 1: Locate the call site**

Run: `grep -rn "process_dragged_paths" durin/cli/`

- [ ] **Step 2: After copying, transcribe audio media**

Where `media` is populated from `process_dragged_paths`, add (pseudocode — adapt to the real call site):

```python
from durin.service.transcription import TranscriptionService

svc = TranscriptionService.from_config(config.transcription)
audio_exts = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus"}
transcribed = []
for m in media:
    if Path(m).suffix.lower() in audio_exts and config.transcription.mode != "off":
        result = await svc.transcribe_and_cache(workspace / m)
        if result.text and config.transcription.mode == "auto":
            text += f'\n[transcripción]: "{result.text}"'
            continue  # do not pass the audio path to the loop as media
    transcribed.append(m)
media = transcribed
```

- [ ] **Step 3: Manual test**

Drag an audio file into the TUI; confirm the transcript is quoted in the input text.

- [ ] **Step 4: Commit**

```bash
git add <tui entrypoint files>
git commit -m "feat(tui): transcribe dragged audio before sending"
```

---

## Task 15: TUI — `/voice` recorder command

**Files:**
- Modify: `durin/cli/tui/command_registry.py` (register the command) and create `durin/cli/tui/voice.py`

- [ ] **Step 1: Create the recorder module**

Create `durin/cli/tui/voice.py`:

```python
"""TUI /voice command — record audio via sounddevice and transcribe it."""

from __future__ import annotations

import hashlib
import tempfile
import wave
from pathlib import Path


def _import_sd():
    try:
        import sounddevice as sd
        return sd
    except ImportError as e:
        raise RuntimeError(
            "Recording needs the [voice] extra: pip install durin-agent[voice] "
            "(Linux also needs libportaudio2)"
        ) from e


def record_wav(max_seconds: int = 120, fs: int = 16000) -> Path:
    """Block until recording finishes; return the WAV path."""
    sd = _import_sd()
    import numpy as np  # sounddevice needs numpy
    print("🔴 Recording… press Enter to stop.")
    frames = sd.rec(int(max_seconds * fs), samplerate=fs, channels=1, dtype="int16")
    input()  # press Enter to stop
    sd.stop()
    data = frames[~np.all(frames == 0, axis=1)]  # trim trailing silence
    tmp = Path(tempfile.gettempdir()) / f"durin-voice-{hashlib.sha256(data.tobytes()).hexdigest()[:12]}.wav"
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(data.tobytes())
    return tmp
```

- [ ] **Step 2: Register the command**

In the TUI command registry, register `/voice` to:
1. `record_wav()`
2. Copy the WAV to `workspace/.media/`
3. Call `TranscriptionService.transcribe_and_cache(path)`
4. Insert the transcript into the input buffer (auto mode) or show a preview (preview mode).

- [ ] **Step 3: Manual test**

Run `durin agent --tui`, type `/voice`, speak, press Enter, confirm transcript appears.

- [ ] **Step 4: Commit**

```bash
git add durin/cli/tui/voice.py durin/cli/tui/command_registry.py
git commit -m "feat(tui): add /voice recorder command"
```

---

## Task 16: Documentation

**Files:**
- Modify: `docs/INSTALL.md`, `README.md`

- [ ] **Step 1: Add an "Audio transcription" section to `docs/INSTALL.md`**

Cover: `pip install durin-agent[stt]` (local Whisper), `pip install durin-agent[voice]` (TUI mic), PortAudio on Linux (`apt install libportaudio2`), and the `transcription.*` config pointer.

- [ ] **Step 2: Add a bullet to `README.md` "Day-to-day"**

```markdown
- Attach or record audio — it's transcribed to text before reaching the agent
  (`[stt]` extra; see [docs/INSTALL.md](docs/INSTALL.md))
```

- [ ] **Step 3: Commit**

```bash
git add docs/INSTALL.md README.md
git commit -m "docs: document audio transcription setup"
```

---

## Task 17: Full-suite verification

- [ ] **Step 1: Run all backend tests**

Run: `pytest -q`
Expected: all green.

- [ ] **Step 2: Run all frontend tests**

Run: `cd webui && bun run test`
Expected: all green.

- [ ] **Step 3: Run `durin doctor`**

Run: `durin doctor`
Expected: `stt.installed` ✓ (with `[stt]` installed), `stt.cloud_keys` ✓/⚠ depending on config.

- [ ] **Step 4: End-to-end smoke**

1. webui: attach an mp3 → transcript appears in input → send → agent replies to the text.
2. webui: record via mic → same.
3. TUI: drag a wav → transcript quoted.
4. TUI: `/voice` → transcript.
5. Telegram: send a voice message → transcript; confirm `.ogg` retained under `~/.durin/media/telegram/`.

- [ ] **Step 5: Final commit (if any docs/tests touched)**

```bash
git add -A
git commit -m "test: verify full audio-transcription suite"
```

---

## Notes for the implementer

- **Confirm exact identifiers** (root config class name, `_UPLOAD_MIME_ALLOWED` construction, the websocket `_dispatch_envelope` signature, the TUI entrypoint that calls `process_dragged_paths`) by grepping before editing — the spec and this plan cite line numbers from the `main` branch at plan-writing time.
- **The `get_media_dir` import in Task 4 may be unused** — drop it if the service never resolves the media dir itself (the caller passes the path).
- **TDD strictly** — each task writes the failing test first, then the implementation, then commits. Do not batch tasks.
- **Frequent commits** — every task ends in a commit so progress is recoverable.
