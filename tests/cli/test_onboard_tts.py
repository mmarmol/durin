# tests/cli/test_onboard_tts.py
from durin.cli.onboard_wizard import _reconcile_extras_from_config
from durin.config.schema import Config


def test_local_tts_adds_tts_extra():
    cfg = Config()
    cfg.tts.enabled = True
    cfg.tts.provider = "local"
    extras: set[str] = set()
    _reconcile_extras_from_config(cfg, extras)
    assert "tts" in extras


def test_cloud_tts_does_not_add_tts_extra():
    cfg = Config()
    cfg.tts.enabled = True
    cfg.tts.provider = "openai"
    extras: set[str] = set()
    _reconcile_extras_from_config(cfg, extras)
    assert "tts" not in extras
