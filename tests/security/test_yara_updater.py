"""Tests for the YARA rule updater (durin/security/yara_updater.py).

A failed/unsafe update must NEVER replace the active rule set. Skips when the
[skill-yara] extra is absent (the updater compile-checks via yara).
"""
import pytest

pytest.importorskip("yara")
from durin.security import yara_updater


def test_refresh_rejects_bad_checksum(tmp_path, monkeypatch):
    dest = tmp_path / "rules"
    dest.mkdir()
    (dest / "old.yar").write_text("rule keep { condition: true }\n")
    monkeypatch.setattr(yara_updater, "_fetch", lambda url, pin: b"rule x { condition: true }\n")
    ok = yara_updater.refresh_rules(dest, "https://feed", "pin", sha256="deadbeef")
    assert ok is False
    assert (dest / "old.yar").exists()  # active set untouched


def test_refresh_rejects_noncompiling(tmp_path, monkeypatch):
    dest = tmp_path / "rules"
    dest.mkdir()
    (dest / "old.yar").write_text("rule keep { condition: true }\n")
    monkeypatch.setattr(yara_updater, "_fetch", lambda url, pin: b"this is not a yara rule {{{")
    monkeypatch.setattr(yara_updater, "_sha256", lambda b: "match")
    ok = yara_updater.refresh_rules(dest, "https://feed", "pin", sha256="match")
    assert ok is False
    assert (dest / "old.yar").exists()


def test_refresh_success_swaps(tmp_path, monkeypatch):
    dest = tmp_path / "rules"
    dest.mkdir()
    monkeypatch.setattr(yara_updater, "_fetch", lambda url, pin: b"rule good { condition: true }\n")
    monkeypatch.setattr(yara_updater, "_sha256", lambda b: "match")
    ok = yara_updater.refresh_rules(dest, "https://feed", "pin", sha256="match")
    assert ok is True
    assert any(dest.glob("*.yar"))


def test_is_stale_no_marker_is_stale(tmp_path):
    dest = tmp_path / "rules"
    dest.mkdir()
    assert yara_updater.is_stale(dest, 24) is True


def test_is_stale_disabled_when_zero(tmp_path):
    dest = tmp_path / "rules"
    dest.mkdir()
    assert yara_updater.is_stale(dest, 0) is False
