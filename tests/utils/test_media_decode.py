"""Tests for ``durin.utils.media_decode``."""

from __future__ import annotations

import base64

import pytest

from durin.utils.media_decode import (
    DEFAULT_MAX_BYTES,
    MAX_FILE_SIZE,
    FileSizeExceeded,
    save_base64_data_url,
)


def _data_url(payload: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def test_saves_png_with_correct_extension(tmp_path) -> None:
    result = save_base64_data_url(_data_url(b"fake png"), tmp_path)
    assert result is not None
    assert result.endswith(".png")
    assert (tmp_path / result.split("/")[-1]).read_bytes() == b"fake png"


def test_returns_none_for_malformed_data_url(tmp_path) -> None:
    assert save_base64_data_url("not-a-data-url", tmp_path) is None


def test_returns_none_for_broken_base64(tmp_path) -> None:
    # Python's b64decode strips non-alphabet chars by default, so we need a
    # payload whose alphabet-filtered length breaks padding.
    assert save_base64_data_url("data:image/png;base64,not-valid-base64!!!", tmp_path) is None


@pytest.mark.parametrize("mime", [
    "audio/mpeg", "audio/ogg", "audio/opus", "audio/wav",
    "audio/webm", "audio/x-m4a", "audio/aac", "audio/flac",
])
def test_every_accepted_audio_is_saved_as_audio(tmp_path, mime, monkeypatch) -> None:
    """The saved extension is how the agent loop tells audio from other
    files; saved as ``.bin``, a recording would read as an unknown file.
    The platform's mimetypes table varies (Linux's knows no ``.weba``), so
    this holds with a table that knows nothing."""
    import mimetypes

    from durin.utils.helpers import is_audio_path

    monkeypatch.setattr(mimetypes, "guess_extension", lambda *a, **k: None)
    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: (None, None))

    result = save_base64_data_url(_data_url(b"sound", mime=mime), tmp_path)
    assert result is not None
    assert is_audio_path(result)


def test_unknown_mime_falls_back_to_bin(tmp_path) -> None:
    result = save_base64_data_url(_data_url(b"xyz", mime="unknown/type"), tmp_path)
    assert result is not None
    assert result.endswith(".bin")


def test_default_limit_is_10mb(tmp_path) -> None:
    """Backwards-compatible default — the API path depends on this."""
    assert DEFAULT_MAX_BYTES == 10 * 1024 * 1024
    assert MAX_FILE_SIZE == 10 * 1024 * 1024

    oversized = b"x" * (11 * 1024 * 1024)
    with pytest.raises(FileSizeExceeded, match="10MB limit"):
        save_base64_data_url(_data_url(oversized), tmp_path)


def test_explicit_max_bytes_overrides_default(tmp_path) -> None:
    """WS channel passes 8 MB; a 9 MB payload should be rejected there even
    though it would pass the 10 MB API limit."""
    payload = b"y" * (9 * 1024 * 1024)
    with pytest.raises(FileSizeExceeded, match="8MB limit"):
        save_base64_data_url(_data_url(payload), tmp_path, max_bytes=8 * 1024 * 1024)


def test_saved_file_lives_under_media_dir(tmp_path) -> None:
    result = save_base64_data_url(_data_url(b"ok"), tmp_path)
    assert result is not None
    assert result.startswith(str(tmp_path))


def test_the_saved_file_keeps_the_senders_name_behind_a_unique_prefix(tmp_path) -> None:
    """The agent names a file by its saved name, so a random one
    ("3f9a1c2b7d4e.pdf") hid which of the user's files it was. The sender's
    name stays, after a unique prefix, so two files with one name never
    collide."""
    from pathlib import Path

    pdf = _data_url(b"%PDF-1.4", mime="application/pdf")
    first = Path(save_base64_data_url(pdf, tmp_path, name="informe Q3 año.pdf",
                                      name_sets_extension=True))
    second = Path(save_base64_data_url(pdf, tmp_path, name="informe Q3 año.pdf",
                                       name_sets_extension=True))
    assert first.name.endswith("_informe-Q3-año.pdf")
    assert first != second and first.parent == tmp_path


def test_a_media_name_never_overrides_the_extension_its_mime_sets(tmp_path) -> None:
    """An image or a recording is read by its content type: a name like
    ``photo.jpeg`` on PNG bytes, or a recording named without a suffix,
    keeps the extension its MIME gives."""
    from pathlib import Path

    from durin.utils.helpers import is_audio_path

    png = Path(save_base64_data_url(_data_url(b"png"), tmp_path, name="photo.jpeg"))
    assert png.name.endswith("_photo.png")
    wav = save_base64_data_url(_data_url(b"sound", mime="audio/wav"), tmp_path, name="recording")
    assert wav.endswith("_recording.wav") and is_audio_path(wav)


def test_a_name_cannot_break_out_of_the_line_it_is_shown_on(tmp_path) -> None:
    """The name reaches the agent's text and a file path: line breaks,
    control characters and path separators become dashes, and a long name
    is cut to a bounded size."""
    from pathlib import Path

    saved = Path(save_base64_data_url(
        _data_url(b"%PDF", mime="application/pdf"), tmp_path,
        name="../a\nb\x00c" + "é" * 300 + ".pdf", name_sets_extension=True,
    ))
    assert saved.parent == tmp_path
    assert not any(ch in saved.name for ch in ("\n", "\x00", "/"))
    assert saved.suffix == ".pdf" and len(saved.name.encode()) <= 160
