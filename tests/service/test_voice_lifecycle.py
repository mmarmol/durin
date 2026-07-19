"""Voice lifecycle: download-verified boot, lazy load, idle unload."""
from __future__ import annotations

import time

import pytest

from durin.service.speech import SpeechSynthesisService
from durin.service.transcription import TranscriptionService


class _FakeTts:
    def __init__(self) -> None:
        self.warmups = 0

    async def warmup(self) -> None:
        self.warmups += 1

    async def synthesize(self, text, *, voice=None, language=None):
        from durin.providers.speech import SpeechAudio

        return SpeechAudio(b"wav", 1)


@pytest.mark.asyncio
async def test_predownload_builds_once_then_marker_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    built: list[_FakeTts] = []

    def factory():
        built.append(_FakeTts())
        return built[-1]

    svc = SpeechSynthesisService(factory, enabled=True, provider_name="local")
    await svc.predownload()
    assert built[0].warmups == 1
    assert svc._provider is None           # engine NOT left resident
    assert (tmp_path / "voice-verified" / "tts-local.ok").exists()

    await svc.predownload()                # marker short-circuits
    assert len(built) == 1

    fresh = SpeechSynthesisService(factory, enabled=True, provider_name="local")
    await fresh.predownload()              # a NEW service also short-circuits
    assert len(built) == 1


@pytest.mark.asyncio
async def test_first_use_loads_lazily_and_idle_unloads(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    svc = SpeechSynthesisService(lambda: _FakeTts(), enabled=True)
    assert svc._provider is None
    await svc.synthesize("hola")
    assert svc._provider is not None       # lazy-loaded on first use

    # Fresh use → not idle yet.
    assert svc.unload_if_idle(900) is False
    # Simulate a long-idle engine.
    svc._last_used = time.monotonic() - 1000
    assert svc.unload_if_idle(900) is True
    assert svc._provider is None
    # Disabled timer never unloads.
    await svc.synthesize("hola")
    svc._last_used = time.monotonic() - 10**6
    assert svc.unload_if_idle(0) is False


@pytest.mark.asyncio
async def test_transcription_mirror_predownload_and_unload(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))

    class _FakeStt:
        def __init__(self) -> None:
            self.warmups = 0

        async def warmup(self) -> None:
            self.warmups += 1

    built: list[_FakeStt] = []

    def factory():
        built.append(_FakeStt())
        return built[-1]

    svc = TranscriptionService(
        provider_factory=factory, enabled=True, provider_name="local")
    await svc.predownload()
    assert built[0].warmups == 1
    assert svc._provider is None
    assert (tmp_path / "voice-verified" / "stt-local.ok").exists()
    await svc.predownload()
    assert len(built) == 1

    svc._provider = factory()
    svc._last_used = time.monotonic() - 1000
    assert svc.unload_if_idle(900) is True
    assert svc._provider is None
