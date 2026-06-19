"""Tests that stt/voice extras are registered for webui gating + onboard."""

from durin.extras import REGISTRY


def test_stt_extra_registered():
    """The [stt] feature must be in the registry so the webui can gate the
    mic/attach UI on whether local transcription is available."""
    assert "stt" in REGISTRY
    fe = REGISTRY["stt"]
    assert fe.extra == "stt"
    assert fe.module == "faster_whisper"
    assert fe.label  # non-empty human label


def test_voice_extra_registered():
    """The [voice] feature (sounddevice) must be registered so the TUI /voice
    command and webui mic can offer to install it."""
    assert "voice" in REGISTRY
    fe = REGISTRY["voice"]
    assert fe.extra == "voice"
    assert fe.module == "sounddevice"
    assert fe.label
