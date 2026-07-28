"""Voice credentials are credentials: they may be ${secret:NAME} references.

Two services build a provider straight from config — the shared transcription
service (four call sites: CLI, TUI twice, ChannelManager) and the TTS service.
Both handed the raw config string to the provider, so a key stored the
documented way arrived as the literal "${secret:NAME}" and auth failed.

These use the real secret store rather than a patched resolver: the modules
bind ``resolve_secret`` at import time, so patching the source module proves
nothing about what they actually call.
"""

from __future__ import annotations

import pytest

from durin.config.schema import Config
from durin.security.secrets import SecretStore, get_secret_store
from durin.service.speech import _build_provider
from durin.service.transcription import TranscriptionService

REAL = "sk-the-real-key"


@pytest.fixture(autouse=True)
def _seeded_store():
    """DURIN_HOME is per-test (conftest), so this writes a throwaway store."""
    store = SecretStore().load()
    store.put("VOICE_KEY", value=REAL, service="probe", scope=["probe"],
              origin="test")
    get_secret_store(reload=True)
    yield
    get_secret_store(reload=True)


def _provider_key(svc) -> str | None:
    prov = svc._factory()
    return getattr(prov, "api_key", None) or getattr(prov, "_api_key", None)


@pytest.mark.parametrize("provider", ["groq", "openai"])
def test_transcription_key_is_resolved(provider):
    cfg = Config().transcription
    cfg.enabled = True
    cfg.provider = provider
    getattr(cfg, provider).api_key = "${secret:VOICE_KEY}"
    assert _provider_key(TranscriptionService.from_config(cfg)) == REAL


def test_transcription_literal_key_still_passes_through():
    cfg = Config().transcription
    cfg.enabled = True
    cfg.provider = "groq"
    cfg.groq.api_key = "gsk-plain-literal"
    assert _provider_key(TranscriptionService.from_config(cfg)) == "gsk-plain-literal"


def test_tts_openai_key_is_resolved():
    cfg = Config().tts
    cfg.openai.api_key = "${secret:VOICE_KEY}"
    prov = _build_provider("openai", cfg)
    key = getattr(prov, "api_key", None) or getattr(prov, "_api_key", None)
    assert key == REAL
    assert not str(key).startswith("${secret:")
