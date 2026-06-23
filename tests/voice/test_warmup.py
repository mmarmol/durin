import pytest

from durin.voice.warmup import warm_speech_services


class _Cfg:
    def __init__(self, enabled=True, provider="local"):
        self.enabled = enabled
        self.provider = provider


class _Svc:
    def __init__(self, boom=False):
        self.boom = boom
        self.warmed = False

    async def warmup(self):
        if self.boom:
            raise RuntimeError("model download failed")
        self.warmed = True


@pytest.mark.asyncio
async def test_cloud_engine_warms_without_extra_check():
    # A non-local provider warms to a no-op success; no extra is required.
    stt = _Svc()
    results = await warm_speech_services(
        stt, _Cfg(provider="openai"), None, _Cfg(enabled=False)
    )
    assert ("Transcription", True, None) in results
    assert stt.warmed is True
    # The disabled TTS was not attempted.
    assert all(label != "Speech synthesis" for label, _, _ in results)


@pytest.mark.asyncio
async def test_disabled_engine_is_skipped():
    stt = _Svc()
    results = await warm_speech_services(
        stt, _Cfg(enabled=False), None, _Cfg(enabled=False)
    )
    assert results == []
    assert stt.warmed is False


@pytest.mark.asyncio
async def test_local_engine_skipped_when_extra_absent(monkeypatch):
    monkeypatch.setattr("durin.extras._module_present", lambda m: False)
    stt = _Svc()
    results = await warm_speech_services(
        stt, _Cfg(provider="local"), None, _Cfg(enabled=False)
    )
    assert results == []
    assert stt.warmed is False


@pytest.mark.asyncio
async def test_local_engine_warmed_when_extra_present(monkeypatch):
    monkeypatch.setattr("durin.extras._module_present", lambda m: True)
    tts = _Svc()
    results = await warm_speech_services(
        None, _Cfg(enabled=False), tts, _Cfg(provider="local")
    )
    assert ("Speech synthesis", True, None) in results
    assert tts.warmed is True


@pytest.mark.asyncio
async def test_warmup_failure_is_captured_not_raised(monkeypatch):
    monkeypatch.setattr("durin.extras._module_present", lambda m: True)
    boom = _Svc(boom=True)
    results = await warm_speech_services(
        boom, _Cfg(provider="local"), None, _Cfg(enabled=False)
    )
    assert len(results) == 1
    label, ok, err = results[0]
    assert label == "Transcription"
    assert ok is False
    assert "model download failed" in err
