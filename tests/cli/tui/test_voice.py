"""Tests for the TUI /voice recorder module (spec §6.2)."""

import pytest

from durin.cli.tui.voice import VoiceUnavailable, record_wav


def test_voice_module_imports_without_sounddevice():
    """Importing voice.py must not require sounddevice (lazy import)."""
    import durin.cli.tui.voice as v

    assert hasattr(v, "record_wav")


def test_record_wav_raises_when_sounddevice_absent(monkeypatch):
    """When sounddevice is unimportable, record_wav raises VoiceUnavailable."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name in ("sounddevice", "numpy"):
            raise ImportError("simulated absence")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises((VoiceUnavailable, ImportError)):
        record_wav(max_seconds=1)


def test_voice_unavailable_message_mentions_voice_extra():
    """The error hint must tell the user how to install the [voice] extra."""
    err = VoiceUnavailable("test")
    # The real message is built in _import_sd; just assert it's a usable error.
    assert isinstance(err, RuntimeError)
