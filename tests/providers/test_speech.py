import io
import wave
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from durin.providers.speech import OpenAISpeechProvider, SpeechAudio


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
