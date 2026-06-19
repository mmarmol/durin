"""Tests for the STT doctor checks (spec §8.1)."""

from durin.cli.doctor import check_stt_installed


def test_doctor_reports_stt_installed_when_faster_whisper_present():
    """When faster_whisper imports, the check passes."""
    import sys
    import types

    sys.modules.setdefault("faster_whisper", types.ModuleType("faster_whisper"))
    result = check_stt_installed()
    assert result.status == "ok"
    assert "faster_whisper" in result.name
    sys.modules.pop("faster_whisper", None)


def test_doctor_reports_stt_missing_when_absent(monkeypatch):
    """When faster_whisper is unimportable, the check warns with the [stt] hint."""
    import importlib

    real_import_module = importlib.import_module

    def fake_import_module(name, *a, **k):
        if name == "faster_whisper":
            raise ImportError("nope")
        return real_import_module(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    result = check_stt_installed()
    assert result.status == "warn"
    assert result.extra == "stt"
    # The fix hint must mention the [stt] extra so users know what to install.
    assert result.fix is not None
    assert "stt" in result.fix
