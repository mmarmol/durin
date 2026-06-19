"""TUI ``/voice`` command — record audio via sounddevice and return a WAV path.

Spec §6.2: recording is cross-platform (Linux/Win/Mac) via the optional
``[voice]`` extra (``sounddevice`` + PortAudio). The recorded WAV is staged
in the workspace ``.media/`` dir and handed back to the caller, which then
runs it through :class:`TranscriptionService` exactly like a dragged-in file.
"""

from __future__ import annotations

import hashlib
import tempfile
import wave
from pathlib import Path

__all__ = ["record_wav", "VoiceUnavailableError"]


class VoiceUnavailableError(RuntimeError):
    """Raised when the ``[voice]`` extra (sounddevice) is not installed."""


def _import_sd():
    try:
        import sounddevice as sd

        return sd
    except ImportError as e:
        raise VoiceUnavailableError(
            "Recording needs the [voice] extra: pip install durin-agent[voice] "
            "(Linux also needs libportaudio2)"
        ) from e


def record_wav(
    *,
    max_seconds: int = 120,
    fs: int = 16000,
    on_stop: "object | None" = None,
) -> Path:
    """Block until recording finishes; return the WAV path.

    Recording stops when ``on_stop()`` returns truthy (polled each buffer) or
    when ``max_seconds`` elapses. When ``on_stop`` is ``None`` the caller is
    responsible for stopping the stream externally — in the TUI we poll a flag
    set by the Enter key.

    The WAV is written to the system temp dir with a content-hash name so
    repeated silence doesn't pile up identical files.
    """
    import numpy as np

    sd = _import_sd()
    frames = sd.rec(int(max_seconds * fs), samplerate=fs, channels=1, dtype="int16")
    # Poll until asked to stop or recording completes naturally.
    if on_stop is not None:
        import time

        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            if on_stop():
                break
            time.sleep(0.05)
    sd.stop()
    # Trim trailing zeros (silence past the spoken audio).
    nonzero = np.where(np.any(frames != 0, axis=1))[0]
    if nonzero.size > 0:
        data = frames[: nonzero[-1] + 1]
    else:
        data = frames
    digest = hashlib.sha256(data.tobytes()).hexdigest()[:12]
    tmp = Path(tempfile.gettempdir()) / f"durin-voice-{digest}.wav"
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(data.tobytes())
    return tmp
