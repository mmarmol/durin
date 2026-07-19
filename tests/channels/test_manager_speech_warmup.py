"""ChannelManager warms the shared STT/TTS engines at startup so the first
transcription / voice synth doesn't pay the model load inline. A subsystem is
warmed only when enabled (cloud providers warm to a no-op). When an enabled
local subsystem's extra is *not installed*, the manager downloads it first
(gated by config.install.auto_install_extras) rather than deferring to use-time;
if the install is disabled or fails, warmup is skipped without raising."""
import types

import pytest

import durin.channels.manager as mgr


def _ok_install(feature, *, config):
    return types.SimpleNamespace(status="installed", needs_restart=False)


class _FakeSvc:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.warmed = 0

    async def predownload(self):
        # Boot verifies model files exist; it never leaves an engine resident.
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
async def test_installs_absent_extra_then_warms(monkeypatch):
    # Enabled but the local extra is missing: download it, then warm once the
    # module becomes importable.
    installed = {"sherpa_onnx": False, "supertonic": False}
    monkeypatch.setattr("durin.extras._module_present", lambda mod: installed.get(mod, False))
    seen: list[str] = []

    def fake_ensure(feature, *, config):
        installed[{"stt": "sherpa_onnx", "tts": "supertonic"}[feature]] = True
        seen.append(feature)
        return types.SimpleNamespace(status="installed", needs_restart=False)

    monkeypatch.setattr("durin.extras.ensure_or_note", fake_ensure)
    m = _manager()
    await m._warmup_speech()
    assert sorted(seen) == ["stt", "tts"]
    assert m.transcription.warmed == 1
    assert m.speech_synthesis.warmed == 1


@pytest.mark.asyncio
async def test_absent_extra_skips_warm_when_install_unavailable(monkeypatch):
    # Auto-install disabled or failed: no warm, no crash (the module stays absent).
    monkeypatch.setattr("durin.extras._module_present", lambda mod: False)

    def fake_ensure(feature, *, config):
        return types.SimpleNamespace(status="disabled", needs_restart=True)

    monkeypatch.setattr("durin.extras.ensure_or_note", fake_ensure)
    m = _manager()
    await m._warmup_speech()
    assert m.transcription.warmed == 0
    assert m.speech_synthesis.warmed == 0


@pytest.mark.asyncio
async def test_present_extra_is_not_reinstalled(monkeypatch):
    monkeypatch.setattr("durin.extras._module_present", lambda mod: True)

    def boom(feature, *, config):  # would fail the test if install were attempted
        raise AssertionError(f"unexpected install of {feature}")

    monkeypatch.setattr("durin.extras.ensure_or_note", boom)
    m = _manager()
    await m._warmup_speech()
    assert m.transcription.warmed == 1
    assert m.speech_synthesis.warmed == 1


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
