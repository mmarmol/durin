import tomllib
from pathlib import Path


def test_ocr_extra_declared():
    data = tomllib.loads(Path("pyproject.toml").read_text())
    extras = data["project"]["optional-dependencies"]
    assert "ocr" in extras
    joined = " ".join(extras["ocr"]).lower()
    assert "rapidocr" in joined


def test_ocr_in_extras_registry():
    # The API (/api/v1/extras/status, ensure) + webui "Install [ocr]" button
    # resolve the feature via durin.extras.REGISTRY — it must carry "ocr".
    from durin.extras import REGISTRY

    assert "ocr" in REGISTRY
    fe = REGISTRY["ocr"]
    assert fe.extra == "ocr"
    assert fe.module == "rapidocr"
    assert fe.needs_restart is True
