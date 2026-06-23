import pytest

from durin.config.schema import TtsConfig
from durin.providers.speech import SpeechAudio, SpeechSynthesisProvider
from durin.service.speech import SpeechSynthesisService


class _Stub(SpeechSynthesisProvider):
    def __init__(self, data=b"AUDIO"):
        self._data = data
        self.calls = 0
        self.warmed = 0

    async def synthesize(self, text, *, voice=None, language=None):
        self.calls += 1
        return SpeechAudio(self._data, 22050)

    async def warmup(self):
        self.warmed += 1


@pytest.mark.asyncio
async def test_service_synthesize_uses_provider():
    stub = _Stub(b"AUDIO")
    svc = SpeechSynthesisService(provider_factory=lambda: stub)
    out = await svc.synthesize("hola")
    assert out.data == b"AUDIO"
    assert stub.calls == 1


@pytest.mark.asyncio
async def test_service_disabled_returns_empty():
    svc = SpeechSynthesisService(provider_factory=lambda: _Stub(), enabled=False)
    out = await svc.synthesize("hola")
    assert out.data == b""


@pytest.mark.asyncio
async def test_service_blank_text_returns_empty():
    stub = _Stub()
    svc = SpeechSynthesisService(provider_factory=lambda: stub)
    out = await svc.synthesize("   ")
    assert out.data == b""
    assert stub.calls == 0


@pytest.mark.asyncio
async def test_service_warmup_delegates_to_provider():
    stub = _Stub()
    svc = SpeechSynthesisService(provider_factory=lambda: stub)
    await svc.warmup()
    assert stub.warmed == 1
    assert stub.calls == 0  # warmup must not synthesize


@pytest.mark.asyncio
async def test_service_warmup_noop_when_disabled():
    stub = _Stub()
    svc = SpeechSynthesisService(provider_factory=lambda: stub, enabled=False)
    await svc.warmup()
    assert stub.warmed == 0


def test_from_config_local_builds_local_provider():
    svc = SpeechSynthesisService.from_config(TtsConfig())  # provider=local, fallback=none
    prov = svc._get()
    assert prov.__class__.__name__ == "LocalSupertonicProvider"


def test_from_config_wraps_fallback():
    cfg = TtsConfig.model_validate({"provider": "local", "fallback": "openai"})
    svc = SpeechSynthesisService.from_config(cfg)
    prov = svc._get()
    assert prov.__class__.__name__ == "FallbackSpeechProvider"
