"""Tests for TranscriptionService: caching, modes, error handling (spec §4)."""

import asyncio
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

    async def transcribe(self, file_path, on_status=None):
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
async def test_cache_invalidated_on_engine_switch(tmp_path):
    """Switching local engine (parakeet→sensevoice) must invalidate the cache."""
    audio = tmp_path / "a.wav"
    _make_wav(audio)

    fake_parakeet = FakeProvider("parakeet-text")
    svc_parakeet = TranscriptionService(
        provider_factory=lambda: fake_parakeet,
        model_name="parakeet",
        cache_transcripts=True,
    )
    first = await svc_parakeet.transcribe_and_cache(audio)
    assert first.text == "parakeet-text"
    assert first.cached is False

    fake_sensevoice = FakeProvider("sensevoice-text")
    svc_sensevoice = TranscriptionService(
        provider_factory=lambda: fake_sensevoice,
        model_name="sensevoice",
        cache_transcripts=True,
    )
    second = await svc_sensevoice.transcribe_and_cache(audio)
    assert second.text == "sensevoice-text"
    assert second.cached is False
    assert fake_sensevoice.calls == 1  # re-transcribed, not served from cache


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


# ---------------------------------------------------------------------------
# Concurrency: per-call on_status must not clobber the shared provider
# ---------------------------------------------------------------------------


class _RecordingProvider:
    """Provider that records which on_status callback it received per call."""

    def __init__(self):
        self.received: list[object] = []

    async def transcribe(self, file_path, on_status=None):
        self.received.append(on_status)
        if on_status:
            on_status("transcribing", 0, 0)
        return "ok"


@pytest.mark.asyncio
async def test_concurrent_calls_route_phases_to_their_own_callback(tmp_path):
    """Two concurrent transcribe_and_cache calls must each deliver phase events
    only to their own on_status callback — no cross-contamination via shared provider."""
    audio_a = tmp_path / "a.wav"
    audio_b = tmp_path / "b.wav"
    audio_a.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    audio_b.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    prov = _RecordingProvider()
    svc = TranscriptionService(
        provider_factory=lambda: prov,
        cache_transcripts=False,
    )

    phases_a: list[str] = []
    phases_b: list[str] = []

    def cb_a(phase, done, total):
        phases_a.append(phase)

    def cb_b(phase, done, total):
        phases_b.append(phase)

    await asyncio.gather(
        svc.transcribe_and_cache(audio_a, on_status=cb_a),
        svc.transcribe_and_cache(audio_b, on_status=cb_b),
    )

    # Each callback must have received its own phase; no cross-routing.
    assert phases_a == ["transcribing"]
    assert phases_b == ["transcribing"]
    # Provider received two distinct callbacks (not the same object repeated).
    assert len(prov.received) == 2
    assert prov.received[0] is cb_a
    assert prov.received[1] is cb_b
