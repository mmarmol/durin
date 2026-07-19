"""Voice-engine lifecycle helpers shared by the STT and TTS services.

The boot contract for voice is download-verified / load-lazy / unload-idle:
startup guarantees the model files exist (paying the engine build ONCE per
install, then releasing it), first real use loads the engine, and a periodic
sweep drops it again after idle. A resident ~1.2GB of voice models in every
gateway — including headless boxes that never speak — is what the 2026-07-19
boot-RSS investigation found; this module is the antidote.
"""
from __future__ import annotations

from pathlib import Path

__all__ = ["verified_marker_path"]


def verified_marker_path(kind: str, provider: str) -> Path:
    """Marker recording that this (kind, provider)'s model files were
    downloaded + engine-built successfully once on this install. Presence
    short-circuits the boot predownload; deleting the file (or the model
    cache drifting away) just means the next first-use pays the download
    lazily — the marker is an optimization, never a correctness gate."""
    from durin.config.home import durin_home

    safe = provider.replace("/", "_")
    return durin_home() / "voice-verified" / f"{kind}-{safe}.ok"
