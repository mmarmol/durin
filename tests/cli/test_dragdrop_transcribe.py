"""Tests for transcribing dragged-in audio before it reaches the agent (spec §6.1)."""

from pathlib import Path

import pytest

from durin.cli.dragdrop import _AUDIO_EXTS, split_audio_for_transcription


def _make_audio(workspace: Path, name: str) -> str:
    """Return a workspace-relative audio path after writing a stub file."""
    media_dir = workspace / ".media"
    media_dir.mkdir(parents=True, exist_ok=True)
    p = media_dir / name
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    return str(p.relative_to(workspace))


def test_split_separates_audio_from_other_media(tmp_path):
    _make_audio(tmp_path, "voice.opus")
    _make_audio(tmp_path, "pic.png")
    media = [str(tmp_path / ".media" / "voice.opus"), str(tmp_path / ".media" / "pic.png")]
    kept, audio = split_audio_for_transcription(media, tmp_path)
    assert len(audio) == 1
    assert audio[0].endswith(".opus")
    assert all(not m.endswith(".opus") for m in kept)


def test_audio_extensions_recognized():
    assert ".opus" in _AUDIO_EXTS
    assert ".mp3" in _AUDIO_EXTS
    assert ".wav" in _AUDIO_EXTS
    assert ".m4a" in _AUDIO_EXTS


@pytest.mark.asyncio
async def test_transcribe_dragged_audio_appends_transcript(tmp_path, monkeypatch):
    """When an audio path is dragged in, the transcript is appended to the value
    and the audio path is dropped from the media list (so it never reaches the
    agent as raw media)."""
    from durin.cli.dragdrop import transcribe_dragged_audio

    audio_path = tmp_path / ".media" / "voice.opus"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    class FakeSvc:
        async def transcribe_and_cache(self, path):
            from durin.service.transcription import TranscriptResult

            return TranscriptResult(
                text="hola mundo", cached=False, meta_path=None, audio_path=Path(path)
            )

    value, media = await transcribe_dragged_audio(
        value="listen to this",
        media=[str(audio_path)],
        workspace=tmp_path,
        service=FakeSvc(),
    )
    assert "hola mundo" in value
    # Audio path removed from media so the agent loop never sees it as raw media.
    assert all(not m.endswith(".opus") for m in media)


@pytest.mark.asyncio
async def test_transcribe_dragged_audio_off_mode_keeps_path(tmp_path):
    """In 'off' mode the audio path is left in media (no transcription)."""
    from durin.cli.dragdrop import transcribe_dragged_audio

    audio_path = tmp_path / ".media" / "voice.opus"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    class FakeSvc:
        async def transcribe_and_cache(self, path):
            raise AssertionError("should not transcribe in off mode")

    value, media = await transcribe_dragged_audio(
        value="listen",
        media=[str(audio_path)],
        workspace=tmp_path,
        service=FakeSvc(),
        mode="off",
    )
    assert "listen" == value
    assert any(m.endswith(".opus") for m in media)
