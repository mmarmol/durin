import tomllib
from pathlib import Path


def test_tts_extra_declared():
    data = tomllib.loads(Path("pyproject.toml").read_text())
    extras = data["project"]["optional-dependencies"]
    assert "tts" in extras
    joined = " ".join(extras["tts"]).lower()
    assert "supertonic" in joined
    assert "onnxruntime" in joined


def test_tts_in_extras_registry():
    # The API (/api/v1/extras/status, ensure) + webui "Install [tts]" button
    # resolve the feature via durin.extras.REGISTRY — it must carry "tts".
    from durin.extras import REGISTRY

    assert "tts" in REGISTRY
    fe = REGISTRY["tts"]
    assert fe.extra == "tts"
    assert fe.module == "supertonic"
    assert fe.needs_restart is True
