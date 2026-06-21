"""Tests for _build_user_content handling audio in transcription 'off' mode.

In 'off' mode the audio is NOT transcribed; instead it reaches the model as
an ``input_audio`` content block (OpenAI-compat) so a multimodal model can
handle it natively. Capability-gated: if the model lacks audio input, the
audio is dropped with a textual note (mirrors opencode's pattern).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.agent.context import ContextBuilder


@pytest.fixture()
def builder(tmp_path: Path) -> ContextBuilder:
    return ContextBuilder(workspace=tmp_path, timezone="UTC")


def _make_wav(path: Path) -> None:
    # Minimal RIFF/WAVE header so the magic-byte detector recognizes it.
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 16)


def test_build_user_content_image_only_unchanged(builder: ContextBuilder, tmp_path: Path):
    """Sanity: the existing image path is unaffected by the new signature."""
    # A 1x1 PNG.
    png = tmp_path / "i.png"
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
        b"\x00\x00IEND\xaeB`\x82"
    )
    out = builder._build_user_content("hi", [str(png)])
    assert isinstance(out, list)
    assert out[0]["type"] == "image_url"


def test_off_mode_audio_becomes_input_audio(builder: ContextBuilder, tmp_path: Path):
    """In transcription 'off' mode with an audio-capable model, audio is
    embedded as an ``input_audio`` block so the model can handle it natively."""
    wav = tmp_path / "v.wav"
    _make_wav(wav)
    out = builder._build_user_content(
        "describe this",
        [str(wav)],
        audio_mode="off",
        supports_audio_input=True,
    )
    assert isinstance(out, list)
    kinds = [b["type"] for b in out]
    assert "input_audio" in kinds
    assert "text" in kinds


def test_off_mode_audio_dropped_when_model_lacks_audio(
    builder: ContextBuilder, tmp_path: Path,
):
    """If the model can't take audio and we're in 'off' mode, the audio is
    dropped and a textual note is injected (no silent loss)."""
    wav = tmp_path / "v.wav"
    _make_wav(wav)
    out = builder._build_user_content(
        "describe this",
        [str(wav)],
        audio_mode="off",
        supports_audio_input=False,
    )
    # No input_audio block, but the note surfaces what happened.
    if isinstance(out, list):
        kinds = [b.get("type") for b in out]
        assert "input_audio" not in kinds
        text_block = next((b for b in out if b.get("type") == "text"), None)
        assert text_block is not None
        assert "audio" in text_block["text"].lower()
    else:
        assert "audio" in out.lower()


def test_auto_mode_audio_not_inlined(builder: ContextBuilder, tmp_path: Path):
    """In the default 'auto' mode, audio is transcribed upstream — the builder
    must NOT inline it as input_audio (that's the whole point: save tokens)."""
    wav = tmp_path / "v.wav"
    _make_wav(wav)
    out = builder._build_user_content(
        "describe this",
        [str(wav)],
        audio_mode="auto",
        supports_audio_input=True,
    )
    if isinstance(out, list):
        kinds = [b.get("type") for b in out]
        assert "input_audio" not in kinds
    # Text-only fallback is fine too.
