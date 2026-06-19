"""Tests for the TranscriptionProvider protocol + LocalWhisperProvider (spec §4.1/4.2)."""

import sys
import types

import pytest

from durin.providers.transcription import LocalWhisperProvider, TranscriptionProvider


def test_local_provider_is_transcription_provider():
    """Structural check: LocalWhisperProvider must satisfy the Protocol."""
    p = LocalWhisperProvider(model="tiny", device="cpu", compute_type="int8")
    assert isinstance(p, TranscriptionProvider)


def test_local_provider_constructs_without_faster_whisper_installed(monkeypatch):
    """Construction must not import faster-whisper (lazy import)."""
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("simulated absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    p = LocalWhisperProvider(model="tiny", device="cpu", compute_type="int8")
    assert p.model == "tiny"


@pytest.mark.asyncio
async def test_local_provider_transcribe_calls_model(tmp_path):
    """transcribe() lazily imports faster_whisper, runs in a thread, returns stripped text."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    captured: dict = {}

    class FakeSegment:
        def __init__(self, text):
            self.text = text

    class FakeSegments:
        def __iter__(self):
            yield FakeSegment("  hello world  ")

    class FakeModel:
        def __init__(self, *a, **kw):
            captured["init_args"] = (a, kw)

        def transcribe(self, path, **kw):
            captured["transcribe_args"] = kw
            return FakeSegments(), None

    class FakeWhisperModule(types.ModuleType):
        WhisperModel = FakeModel

    sys.modules["faster_whisper"] = FakeWhisperModule("faster_whisper")
    try:
        p = LocalWhisperProvider(
            model="tiny", device="cpu", compute_type="int8", language="en",
        )
        text = await p.transcribe(audio)
    finally:
        sys.modules.pop("faster_whisper", None)

    assert text == "hello world"
    assert captured["transcribe_args"].get("language") == "en"


@pytest.mark.asyncio
async def test_local_provider_transcribe_missing_file(tmp_path):
    """Missing audio file returns empty string, never raises."""
    p = LocalWhisperProvider(model="tiny", device="cpu", compute_type="int8")
    text = await p.transcribe(tmp_path / "nope.wav")
    assert text == ""
