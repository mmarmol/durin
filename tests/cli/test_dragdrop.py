"""Tests for the CLI drag-and-drop pre-processor."""

from __future__ import annotations

import hashlib
from pathlib import Path

from durin.cli.dragdrop import process_dragged_paths


def _make_file(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_no_paths_leaves_text_alone(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    text, media = process_dragged_paths("just a plain message", workspace)
    assert text == "just a plain message"
    assert media == []


def test_empty_input_short_circuit(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    text, media = process_dragged_paths("", workspace)
    assert text == ""
    assert media == []


def test_image_is_copied_and_path_rewritten(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    src = _make_file(tmp_path / "src" / "photo.png", b"\x89PNG\r\n\x1a\nfake")
    text = f"check this image {src} please"

    cleaned, media = process_dragged_paths(text, workspace)

    # The original path is replaced with the workspace-local copy.
    assert str(src) not in cleaned
    assert ".media" in cleaned
    assert "photo.png" not in cleaned  # rename by content hash
    assert len(media) == 1
    assert media[0].startswith(".media/")
    # File was actually copied.
    dest = workspace / media[0]
    assert dest.is_file()
    assert dest.suffix == ".png"


def test_audio_is_copied_too(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    src = _make_file(tmp_path / "src" / "voice.m4a", b"\x00\x00\x00\x20ftyp")
    text = f"transcribe {src}"

    cleaned, media = process_dragged_paths(text, workspace)
    assert len(media) == 1
    assert media[0].endswith(".m4a")
    assert (workspace / media[0]).is_file()


def test_document_path_left_in_place(tmp_path: Path) -> None:
    """Markdown / text / pdf paths stay untouched — read_file handles them."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    src = _make_file(tmp_path / "notes.md", b"# title")
    text = f"summarize {src}"

    cleaned, media = process_dragged_paths(text, workspace)
    assert str(src) in cleaned
    assert media == []
    # No .media dir created when nothing is copied.
    assert not (workspace / ".media").exists()


def test_idempotent_same_content_same_dest(tmp_path: Path) -> None:
    """Re-dragging the same image content resolves to the same copy."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    src = _make_file(tmp_path / "src" / "p.png", b"sameframe")

    _, media1 = process_dragged_paths(f"first {src}", workspace)
    _, media2 = process_dragged_paths(f"second {src}", workspace)

    assert media1 == media2
    # And the hash matches what we'd compute manually.
    expected_hash = hashlib.sha256(b"sameframe").hexdigest()[:16]
    assert media1[0] == f".media/{expected_hash}.png"


def test_multiple_paths_one_message(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    img = _make_file(tmp_path / "i.png", b"i")
    audio = _make_file(tmp_path / "a.wav", b"a")
    doc = _make_file(tmp_path / "n.md", b"d")
    text = f"compare {img} and {audio}, see {doc}"

    cleaned, media = process_dragged_paths(text, workspace)
    assert len(media) == 2
    assert str(img) not in cleaned   # image rewritten
    assert str(audio) not in cleaned  # audio rewritten
    assert str(doc) in cleaned       # doc kept


def test_nonexistent_path_left_alone(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    text = "look at /imaginary/path/x.png"
    cleaned, media = process_dragged_paths(text, workspace)
    assert cleaned == text
    assert media == []


def test_tilde_expansion(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    src = _make_file(fake_home / "shot.png", b"png")

    text = "see ~/shot.png"
    cleaned, media = process_dragged_paths(text, workspace)
    assert len(media) == 1
    assert "~/shot.png" not in cleaned


def test_escaped_space_in_path(tmp_path: Path) -> None:
    """Bash-style escaped space (e.g. iTerm2 paste) resolves to a real file."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    src = _make_file(tmp_path / "with space.png", b"img")
    # iTerm2 escapes the space when you drag a file with a space in the name.
    escaped = str(src).replace(" ", "\\ ")
    text = f"show {escaped}"

    cleaned, media = process_dragged_paths(text, workspace)
    assert len(media) == 1


def test_unsupported_extension_left_alone(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    src = _make_file(tmp_path / "blob.bin", b"\x00\x01")
    text = f"raw {src}"
    cleaned, media = process_dragged_paths(text, workspace)
    assert cleaned == text
    assert media == []


def test_directory_path_left_alone(tmp_path: Path) -> None:
    """Dragging a directory must not trigger copy logic."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subdir = tmp_path / "somedir"
    subdir.mkdir()
    text = f"explain the layout of {subdir}"
    cleaned, media = process_dragged_paths(text, workspace)
    assert cleaned == text
    assert media == []
