import io
import sys
import types
import wave
import wave as _wave
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from durin.providers.speech import (
    FallbackSpeechProvider,
    OpenAISpeechProvider,
    SpeechAudio,
    SpeechSynthesisProvider,
)


def _make_wav_bytes(sample_rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * 200)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_openai_speech_returns_wav():
    prov = OpenAISpeechProvider(api_key="sk-test")
    wav = _make_wav_bytes(24000)
    resp = httpx.Response(
        200, content=wav, request=httpx.Request("POST", "https://x/audio/speech")
    )
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=resp)):
        out = await prov.synthesize("hola mundo", voice=None)
    assert isinstance(out, SpeechAudio)
    assert out.data == wav
    assert out.sample_rate == 24000
    assert out.format == "wav"


@pytest.mark.asyncio
async def test_openai_speech_no_key_returns_empty(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    prov = OpenAISpeechProvider(api_key=None)
    out = await prov.synthesize("hola")
    assert out.data == b""
    assert out.sample_rate == 0


@pytest.fixture
def fake_supertonic(monkeypatch):
    """Inject a fake `supertonic` module whose save_audio writes a real WAV."""

    class _FakeTTS:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_voice_style(self, voice_name):
            return {"name": voice_name}

        def synthesize(self, text, voice_style=None):
            return ("frames", None)

        def save_audio(self, wav, path):
            with _wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(22050)
                w.writeframes(b"\x00\x00" * 100)

    mod = types.ModuleType("supertonic")
    mod.TTS = _FakeTTS
    monkeypatch.setitem(sys.modules, "supertonic", mod)
    return _FakeTTS


@pytest.mark.asyncio
async def test_local_supertonic_synthesizes(fake_supertonic):
    from durin.providers.speech import LocalSupertonicProvider

    prov = LocalSupertonicProvider(voice="F4")
    out = await prov.synthesize("hola mundo")
    assert out.format == "wav"
    assert out.sample_rate == 22050
    assert len(out.data) > 44  # WAV header + frames


@pytest.mark.asyncio
async def test_local_supertonic_missing_extra(monkeypatch):
    from durin.providers.speech import LocalSupertonicProvider

    # Setting the module to None makes `import supertonic` raise ImportError.
    monkeypatch.setitem(sys.modules, "supertonic", None)
    prov = LocalSupertonicProvider()
    with pytest.raises(RuntimeError, match=r"\[tts\] extra"):
        await prov.synthesize("hi")


class _StubProvider(SpeechSynthesisProvider):
    def __init__(self, data=b"PRIMARY", sr=22050, exc=None):
        self._data = data
        self._sr = sr
        self._exc = exc
        self.calls = 0

    async def synthesize(self, text, *, voice=None, language=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return SpeechAudio(self._data, self._sr)


@pytest.mark.asyncio
async def test_fallback_uses_primary_when_ok():
    primary = _StubProvider(b"PRIMARY")
    secondary = _StubProvider(b"CLOUD")
    prov = FallbackSpeechProvider(primary, secondary)
    out = await prov.synthesize("hi")
    assert out.data == b"PRIMARY"
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_fallback_on_exception():
    primary = _StubProvider(exc=RuntimeError("boom"))
    secondary = _StubProvider(b"CLOUD")
    prov = FallbackSpeechProvider(primary, secondary)
    out = await prov.synthesize("hi")
    assert out.data == b"CLOUD"
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_fallback_on_empty_audio():
    primary = _StubProvider(data=b"")
    secondary = _StubProvider(b"CLOUD")
    prov = FallbackSpeechProvider(primary, secondary)
    out = await prov.synthesize("hi")
    assert out.data == b"CLOUD"
