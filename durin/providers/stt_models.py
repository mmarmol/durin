"""Registry + on-demand download of sherpa-onnx STT models.

Models are fetched from the sherpa-onnx GitHub release assets the first
time an engine is used, then cached under ``<cache_dir>/<dir_name>/``.
"""

from __future__ import annotations

import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from loguru import logger

StatusCb = Callable[[str, int, int], None]

_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"


@dataclass(frozen=True)
class EngineSpec:
    dir_name: str          # extracted top-level directory inside the tarball
    tarball: str           # release asset filename (.tar.bz2)
    files: dict[str, str]  # logical name -> filename within dir_name


# Filenames verified 2026-06-20 against:
#   Parakeet: https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/nemo-transducer-models.html
#             and HF repo csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8
#   SenseVoice: https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html
#   Note: SenseVoice dir_name includes "-int8-" before the date (not "-2024-07-17" directly).
ENGINES: dict[str, EngineSpec] = {
    "parakeet": EngineSpec(
        dir_name="sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
        tarball="sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
        files={
            "encoder": "encoder.int8.onnx",
            "decoder": "decoder.int8.onnx",
            "joiner": "joiner.int8.onnx",
            "tokens": "tokens.txt",
        },
    ),
    "sensevoice": EngineSpec(
        dir_name="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        tarball="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        files={
            "model": "model.int8.onnx",
            "tokens": "tokens.txt",
        },
    ),
}


def ensure_model(engine: str, cache_dir: Path, on_status: StatusCb | None = None) -> dict[str, Path]:
    spec = ENGINES.get(engine)
    if spec is None:
        raise ValueError(f"Unknown STT engine: {engine!r} (have {list(ENGINES)})")
    cache_dir = Path(cache_dir)
    eng_dir = cache_dir / spec.dir_name
    resolved = {k: eng_dir / fname for k, fname in spec.files.items()}
    if all(p.exists() for p in resolved.values()):
        return resolved
    cache_dir.mkdir(parents=True, exist_ok=True)
    _download_and_extract(spec, cache_dir, on_status)
    missing = [str(p) for p in resolved.values() if not p.exists()]
    if missing:
        raise RuntimeError(f"STT model {engine!r} incomplete after extract: {missing}")
    return resolved


def _download_and_extract(spec: EngineSpec, cache_dir: Path, on_status: StatusCb | None) -> None:
    url = f"{_RELEASE}/{spec.tarball}"
    tar_path = cache_dir / spec.tarball
    logger.info("Downloading STT model {} from {}", spec.dir_name, url)
    with urllib.request.urlopen(url) as resp:  # noqa: S310 (trusted release host)
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(tar_path, "wb") as fh:
            while chunk := resp.read(1 << 16):
                fh.write(chunk)
                done += len(chunk)
                if on_status:
                    on_status("downloading", done, total)
    try:
        with tarfile.open(tar_path, "r:bz2") as tf:
            try:
                tf.extractall(cache_dir, filter="data")  # safe extract (py3.11.4+/3.12+)
            except TypeError:
                # Python < 3.11.4 lacks the `filter=` kwarg; source is a trusted release asset.
                tf.extractall(cache_dir)
    finally:
        tar_path.unlink(missing_ok=True)
