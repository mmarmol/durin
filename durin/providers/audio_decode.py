"""Decode any audio file to 16 kHz mono float32 PCM for sherpa-onnx.

sherpa-onnx's ``accept_waveform`` needs raw float32 samples; it does not
decode container formats (webm/opus/m4a/ogg/mp3). PyAV (bundled ffmpeg)
handles every format the webui/TUI/channels produce.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

TARGET_RATE = 16000


def decode_to_mono_16k(path: str | Path) -> tuple[np.ndarray, int]:
    import av  # lazy: only needed when the [stt] extra is installed

    container = av.open(str(path))
    try:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=TARGET_RATE)
        chunks: list[np.ndarray] = []
        for frame in container.decode(audio=0):
            for rframe in resampler.resample(frame):
                chunks.append(rframe.to_ndarray().reshape(-1))
        for rframe in resampler.resample(None):  # flush
            chunks.append(rframe.to_ndarray().reshape(-1))
    finally:
        container.close()

    if not chunks:
        return np.zeros(0, dtype=np.float32), TARGET_RATE
    return np.concatenate(chunks).astype(np.float32), TARGET_RATE
