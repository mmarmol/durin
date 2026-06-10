"""Tests for durin.utils.atomic_write."""

from __future__ import annotations

import os
import stat

import pytest

from durin.utils.atomic_write import atomic_write_bytes, atomic_write_text


class TestAtomicWriteText:

    def test_writes_content(self, tmp_path):
        target = tmp_path / "out.txt"
        atomic_write_text(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("old", encoding="utf-8")
        atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "out.txt"
        atomic_write_text(target, "deep")
        assert target.read_text(encoding="utf-8") == "deep"

    def test_no_tmp_leftover_on_success(self, tmp_path):
        target = tmp_path / "out.txt"
        atomic_write_text(target, "x")
        assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]

    def test_preserves_existing_mode(self, tmp_path):
        target = tmp_path / "secret.txt"
        target.write_text("v1", encoding="utf-8")
        os.chmod(target, 0o600)
        atomic_write_text(target, "v2")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_new_file_gets_644(self, tmp_path):
        target = tmp_path / "fresh.txt"
        atomic_write_text(target, "x")
        assert stat.S_IMODE(target.stat().st_mode) == 0o644

    def test_symlink_target_updated_in_place(self, tmp_path):
        real = tmp_path / "real.txt"
        real.write_text("v1", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        atomic_write_text(link, "v2")
        assert link.is_symlink()  # link survives — not replaced by a file
        assert real.read_text(encoding="utf-8") == "v2"

    def test_failure_cleans_tmp_and_keeps_original(self, tmp_path, monkeypatch):
        target = tmp_path / "out.txt"
        target.write_text("original", encoding="utf-8")

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "original"
        assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


class TestAtomicWriteBytes:

    def test_writes_bytes(self, tmp_path):
        target = tmp_path / "out.bin"
        atomic_write_bytes(target, b"\x00\x01\x02")
        assert target.read_bytes() == b"\x00\x01\x02"

    def test_text_encoding_param(self, tmp_path):
        target = tmp_path / "latin.txt"
        atomic_write_text(target, "ñandú", encoding="latin-1")
        assert target.read_bytes() == "ñandú".encode("latin-1")


class TestFsyncOption:

    def test_fsync_false_skips_disk_flush(self, tmp_path, monkeypatch):
        import durin.utils.atomic_write as aw

        calls = []
        real_fsync = os.fsync

        def counting_fsync(fd):
            calls.append(fd)
            return real_fsync(fd)

        monkeypatch.setattr(aw.os, "fsync", counting_fsync)
        target = tmp_path / "hot.md"
        aw.atomic_write_text(target, "derived", fsync=False)
        assert calls == []
        assert target.read_text(encoding="utf-8") == "derived"

    def test_fsync_default_flushes(self, tmp_path, monkeypatch):
        import durin.utils.atomic_write as aw

        calls = []
        real_fsync = os.fsync

        def counting_fsync(fd):
            calls.append(fd)
            return real_fsync(fd)

        monkeypatch.setattr(aw.os, "fsync", counting_fsync)
        aw.atomic_write_text(tmp_path / "durable.md", "x")
        assert len(calls) == 1
