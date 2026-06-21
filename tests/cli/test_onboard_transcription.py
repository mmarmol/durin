"""Tests for the transcription submenu in the onboard wizard (Gap D)."""

from __future__ import annotations

from durin.cli.onboard_wizard import (
    _configure_transcription,
    _reconcile_extras_from_config,
)
from durin.config.schema import Config


class _SeqFakeQ:
    """Fake questionary that returns canned answers in sequence."""

    def __init__(self, answers: list) -> None:
        self._answers = list(answers)
        self.calls: list[str] = []

    def select(self, message, choices=None, default=None) -> "_SeqFakeQ":
        self.calls.append(message)
        return self

    def confirm(self, message, default=None) -> "_SeqFakeQ":
        self.calls.append(message)
        return self

    def ask(self):
        if self._answers:
            return self._answers.pop(0)
        return None  # acts like "Back"/cancel once exhausted


def test_pick_local_provider_adds_stt_extra():
    config = Config()
    extras: set[str] = set()
    summary: list[str] = []
    # Sequence: open menu -> pick "Provider: local" -> choose local provider
    # label -> pick parakeet engine -> then "Back" to exit
    fake = _SeqFakeQ([
        "Provider: local",
        "Local (offline, fast — [stt] extra)",
        "Parakeet v3 — European langs incl. Spanish/English (default)",
        "← Back",
    ])
    _configure_transcription(config, extras, fake, summary)
    assert config.transcription.provider == "local"
    assert "stt" in extras
    assert config.transcription.local.engine == "parakeet"


def test_pick_local_provider_sensevoice_sets_engine():
    """Choosing SenseVoice in the engine submenu sets engine='sensevoice'."""
    config = Config()
    extras: set[str] = set()
    summary: list[str] = []
    fake = _SeqFakeQ([
        "Provider: local",
        "Local (offline, fast — [stt] extra)",
        "SenseVoice — Chinese / Japanese / Korean / Cantonese",
        "← Back",
    ])
    _configure_transcription(config, extras, fake, summary)
    assert config.transcription.provider == "local"
    assert "stt" in extras
    assert config.transcription.local.engine == "sensevoice"


def test_pick_groq_removes_stt_and_sets_provider():
    config = Config()
    config.transcription.provider = "local"
    extras: set[str] = {"stt"}
    fake = _SeqFakeQ(["Provider: local", "Groq (cloud, fast, free tier)", "← Back"])
    _configure_transcription(config, extras, fake, [])
    assert config.transcription.provider == "groq"
    assert "stt" not in extras


def test_toggle_mic_adds_voice_extra():
    config = Config()
    extras: set[str] = set()
    # First enter, toggle mic ON, then back.
    fake = _SeqFakeQ([
        "TUI mic recording (/voice) — off",
        "← Back",
    ])
    _configure_transcription(config, extras, fake, [])
    assert "voice" in extras


def test_engine_back_does_not_commit_local_provider():
    """Picking Back from the engine submenu must not commit local provider or stt extra."""
    config = Config()
    original_provider = config.transcription.provider
    extras: set[str] = set()
    summary: list[str] = []
    # Sequence: open menu -> pick "Provider: local" -> choose local -> pick Back from engine -> Back to exit
    fake = _SeqFakeQ([
        "Provider: local",
        "Local (offline, fast — [stt] extra)",
        "← Back",  # back from engine select
        "← Back",  # back from main menu
    ])
    _configure_transcription(config, extras, fake, summary)
    assert config.transcription.provider == original_provider
    assert "stt" not in extras


def test_reconcile_adds_stt_when_local_provider_enabled():
    config = Config()
    config.transcription.provider = "local"
    config.transcription.enabled = True
    extras: set[str] = set()
    _reconcile_extras_from_config(config, extras)
    assert "stt" in extras


def test_reconcile_no_stt_when_disabled():
    config = Config()
    config.transcription.enabled = False
    extras: set[str] = set()
    _reconcile_extras_from_config(config, extras)
    assert "stt" not in extras


def test_reconcile_no_stt_when_cloud_provider():
    config = Config()
    config.transcription.provider = "groq"
    extras: set[str] = set()
    _reconcile_extras_from_config(config, extras)
    assert "stt" not in extras
