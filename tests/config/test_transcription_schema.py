"""Schema tests for the global transcription config (spec §4.3)."""

import pytest

from durin.config.schema import Config, TranscriptionConfig


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
