"""ChannelManager warms the shared STT/TTS engines at startup so the first
transcription / voice synth doesn't pay the model load inline — but only when
the local extra is installed (cloud providers warm to a no-op), and never for a
disabled subsystem. A missing local extra is skipped silently (the install
prompts still surface at use-time)."""
import types

import pytest

import durin.channels.manager as mgr


class _FakeSvc:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.warmed = 0

    async def warmup(self):
        self.warmed += 1


def _manager(*, tts_provider="local", tts_enabled=True,
             stt_provider="local", stt_enabled=True):
    m = mgr.ChannelManager.__new__(mgr.ChannelManager)
    m.transcription = _FakeSvc(stt_enabled)
    m.speech_synthesis = _FakeSvc(tts_enabled)
    m.config = types.SimpleNamespace(
        transcription=types.SimpleNamespace(provider=stt_provider, enabled=stt_enabled),
        tts=types.SimpleNamespace(provider=tts_provider, enabled=tts_enabled),
    )
    return m


@pytest.mark.asyncio
async def test_warms_local_engines_when_extra_present(monkeypatch):
    monkeypatch.setattr("durin.extras._module_present", lambda m: True)
    m = _manager()
    await m._warmup_speech()
    assert m.transcription.warmed == 1
    assert m.speech_synthesis.warmed == 1


@pytest.mark.asyncio
async def test_skips_local_engines_when_extra_absent(monkeypatch):
    monkeypatch.setattr("durin.extras._module_present", lambda m: False)
    m = _manager()
    await m._warmup_speech()
    assert m.transcription.warmed == 0
    assert m.speech_synthesis.warmed == 0


@pytest.mark.asyncio
async def test_skips_disabled_subsystem(monkeypatch):
    monkeypatch.setattr("durin.extras._module_present", lambda m: True)
    m = _manager(tts_enabled=False)
    await m._warmup_speech()
    assert m.transcription.warmed == 1  # stt enabled → warmed
    assert m.speech_synthesis.warmed == 0  # tts disabled → skipped


@pytest.mark.asyncio
async def test_warms_cloud_provider_without_extra(monkeypatch):
    # A cloud provider needs no local extra; warmup is called (a no-op downstream).
    monkeypatch.setattr("durin.extras._module_present", lambda m: False)
    m = _manager(tts_provider="openai")
    await m._warmup_speech()
    assert m.speech_synthesis.warmed == 1  # cloud → warmed regardless of extra


@pytest.mark.asyncio
async def test_warmup_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr("durin.extras._module_present", lambda m: True)
    m = _manager()

    async def boom():
        raise RuntimeError("model load failed")

    m.speech_synthesis.warmup = boom
    await m._warmup_speech()  # must not raise
    assert m.transcription.warmed == 1
