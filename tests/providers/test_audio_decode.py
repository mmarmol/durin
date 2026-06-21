import math
import wave

import pytest

# The [stt] extra (numpy/av) is omitted in CI; skip the whole module there.
np = pytest.importorskip("numpy")

from durin.providers.audio_decode import decode_to_mono_16k  # noqa: E402


def _write_sine_wav(path, seconds=1.0, sr=44100, freq=440.0):
    n = int(seconds * sr)
    samples = (0.5 * np.sin(2 * math.pi * freq * np.arange(n) / sr) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())


def test_decode_resamples_to_16k_mono_float32(tmp_path):
    wav = tmp_path / "tone.wav"
    _write_sine_wav(wav, seconds=1.0, sr=44100)
    samples, sr = decode_to_mono_16k(wav)
    assert sr == 16000
    assert samples.dtype == np.float32
    assert samples.ndim == 1
    # ~1 s at 16 kHz, allow resampler edge slack
    assert 15000 <= samples.shape[0] <= 17000
    assert float(np.max(np.abs(samples))) <= 1.0
