from pathlib import Path
import durin.providers.stt_models as m


def test_ensure_model_idempotent_when_files_present(tmp_path, monkeypatch):
    # Pre-create the extracted sensevoice files so no download is attempted.
    eng_dir = tmp_path / "sensevoice" / m.ENGINES["sensevoice"].dir_name
    eng_dir.mkdir(parents=True)
    (eng_dir / "model.int8.onnx").write_bytes(b"x")
    (eng_dir / "tokens.txt").write_text("a\n")

    def _boom(*a, **k):
        raise AssertionError("should not download when files exist")

    monkeypatch.setattr(m, "_download_and_extract", _boom)
    out = m.ensure_model("sensevoice", tmp_path / "sensevoice")
    assert out["model"].name == "model.int8.onnx"
    assert out["tokens"].name == "tokens.txt"


def test_ensure_model_unknown_engine(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        m.ensure_model("bogus", tmp_path)
