"""Tests that ChannelManager wires a TranscriptionService into channels (spec §8)."""

from durin.channels.manager import ChannelManager
from durin.config.schema import Config
from durin.service.transcription import TranscriptionService


def test_manager_exposes_transcription_service():
    """The manager builds a shared TranscriptionService from config.transcription."""
    config = Config()
    mgr = ChannelManager(config=config, bus=None)  # bus unused until start()
    assert isinstance(mgr.transcription, TranscriptionService)
    assert mgr.transcription.mode == config.transcription.mode
    assert mgr.transcription.enabled == config.transcription.enabled


def test_manager_disabled_transcription_still_exposes_service():
    """Even when disabled, the service object exists (it short-circuits internally)."""
    config = Config()
    config.transcription.enabled = False
    mgr = ChannelManager(config=config, bus=None)
    assert isinstance(mgr.transcription, TranscriptionService)
    assert mgr.transcription.enabled is False
