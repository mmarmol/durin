"""Tests for the STT doctor checks (spec §8.1)."""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

from durin.cli.doctor import check_stt_installed, check_stt_model_cached


def test_doctor_reports_stt_installed_when_sherpa_onnx_present():
    """When sherpa_onnx imports, the check passes."""
    sys.modules.setdefault("sherpa_onnx", types.ModuleType("sherpa_onnx"))
    result = check_stt_installed()
    assert result.status == "ok"
    assert "sherpa_onnx" in result.name
    sys.modules.pop("sherpa_onnx", None)


def test_doctor_reports_stt_missing_when_absent(monkeypatch):
    """When sherpa_onnx is unimportable, the check warns with the [stt] hint."""
    import importlib

    real_import_module = importlib.import_module

    def fake_import_module(name, *a, **k):
        if name == "sherpa_onnx":
            raise ImportError("nope")
        return real_import_module(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    result = check_stt_installed()
    assert result.status == "warn"
    assert result.extra == "stt"
    # The fix hint must mention the [stt] extra so users know what to install.
    assert result.fix is not None
    assert "stt" in result.fix


def test_check_stt_model_cached_skips_for_cloud_provider():
    """When the provider is not local, the check returns ok."""
    from durin.config.schema import Config
    config = Config()
    config.transcription.provider = "groq"
    result = check_stt_model_cached(cfg=config)
    assert result.status == "ok"
    assert "cloud" in result.message


def test_check_stt_model_cached_ok_when_model_present(tmp_path):
    """When the local engine's tokens file exists, the check returns ok."""
    from durin.config.schema import Config
    from durin.providers.stt_models import ENGINES

    config = Config()
    config.transcription.provider = "local"
    config.transcription.local.engine = "parakeet"

    spec = ENGINES["parakeet"]
    model_dir = tmp_path / spec.dir_name
    model_dir.mkdir()
    (model_dir / spec.files["tokens"]).write_text("dummy")

    with patch("durin.providers.transcription._default_stt_cache", return_value=tmp_path):
        result = check_stt_model_cached(cfg=config)

    assert result.status == "ok"
    assert "parakeet" in result.message


def test_check_stt_model_cached_warns_when_model_absent(tmp_path):
    """When the local engine's tokens file is missing, the check warns."""
    from durin.config.schema import Config

    config = Config()
    config.transcription.provider = "local"
    config.transcription.local.engine = "parakeet"

    # tmp_path is empty — no model files present
    with patch("durin.providers.transcription._default_stt_cache", return_value=tmp_path):
        result = check_stt_model_cached(cfg=config)

    assert result.status == "warn"
    assert "parakeet" in result.message
    assert result.fix is not None
