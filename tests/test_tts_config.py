from durin.config.schema import TtsConfig, TranscriptionConfig


def test_tts_config_defaults():
    c = TtsConfig()
    assert c.enabled is True
    assert c.provider == "local"
    assert c.fallback == "none"
    assert c.local.engine == "supertonic"
    assert c.local.voice == "F4"          # proven default from Local-VoiceMode-LLM


def test_tts_config_parses_overrides():
    c = TtsConfig.model_validate(
        {"provider": "openai", "fallback": "openai", "local": {"voice": "M2"}}
    )
    assert c.provider == "openai"
    assert c.fallback == "openai"
    assert c.local.voice == "M2"


def test_tts_config_independent_of_transcription():
    # Adding TTS must not disturb the existing STT config defaults.
    assert TranscriptionConfig().provider == "local"
