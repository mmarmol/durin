import tomllib
from pathlib import Path


def test_tts_extra_declared():
    data = tomllib.loads(Path("pyproject.toml").read_text())
    extras = data["project"]["optional-dependencies"]
    assert "tts" in extras
    joined = " ".join(extras["tts"]).lower()
    assert "supertonic" in joined
    assert "onnxruntime" in joined
