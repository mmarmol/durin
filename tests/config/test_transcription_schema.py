"""Schema tests for the global transcription config (spec §4.3)."""

import pytest

from durin.config.schema import Config, TranscriptionConfig, TranscriptionLocalConfig


def test_transcription_defaults():
    cfg = TranscriptionConfig()
    assert cfg.enabled is True
    assert cfg.mode == "auto"
    assert cfg.provider == "local"
    assert cfg.language is None
    assert cfg.local.engine == "parakeet"
    assert cfg.max_duration_s == 600
    assert cfg.cache_transcripts is True


def test_local_engine_default_is_parakeet():
    assert TranscriptionLocalConfig().engine == "parakeet"


def test_local_accepts_sensevoice():
    c = TranscriptionLocalConfig(engine="sensevoice", num_threads=4)
    assert c.engine == "sensevoice"
    assert c.num_threads == 4


def test_legacy_whisper_keys_are_ignored_not_errors():
    c = TranscriptionLocalConfig(model="large-v3", device="cpu", compute_type="int8")
    assert c.engine == "parakeet"  # default; legacy keys dropped


def test_transcription_mode_invalid():
    with pytest.raises(Exception):
        TranscriptionConfig(mode="bogus")


def test_transcription_language_pattern():
    with pytest.raises(Exception):
        TranscriptionConfig(language="spanish")  # must be ISO-639-1


def test_root_config_has_transcription():
    root = Config()
    assert isinstance(root.transcription, TranscriptionConfig)


def test_provider_resolution_values_are_accepted():
    for p in ("local", "openai", "groq", "http"):
        TranscriptionConfig(provider=p)
