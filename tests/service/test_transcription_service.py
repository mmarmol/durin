"""Tests for TranscriptionService: caching, modes, error handling (spec §4)."""

from pathlib import Path

import pytest

from durin.service.transcription import (
    TranscriptResult,
    TranscriptionService,
    _resolve_model_name,
)
from durin.config.schema import TranscriptionConfig


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
    svc_a = TranscriptionService(
        provider_factory=lambda: FakeProvider("v1"), model_name="A"
    )
    await svc_a.transcribe_and_cache(audio)
    fake_b = FakeProvider("v2")
    svc_b = TranscriptionService(
        provider_factory=lambda: fake_b, model_name="B"
    )
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


def test_resolve_model_name_local_is_engine():
    cfg = TranscriptionConfig(provider="local")
    cfg.local.engine = "sensevoice"
    assert _resolve_model_name(cfg) == "sensevoice"


def test_local_factory_builds_sttprovider():
    cfg = TranscriptionConfig(provider="local")
    cfg.local.engine = "parakeet"
    svc = TranscriptionService.from_config(cfg)
    prov = svc._get_provider()
    from durin.providers.transcription import LocalSttProvider
    assert isinstance(prov, LocalSttProvider)
    assert prov.engine == "parakeet"
