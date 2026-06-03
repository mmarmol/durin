import json
from pathlib import Path

from durin.logs.reader import compute_facets


def test_gateway_facets(tmp_path: Path):
    (tmp_path / "gateway.log").write_text(
        json.dumps({"record": {"time": {"timestamp": 1.0}, "level": {"name": "INFO"},
                               "extra": {"channel": "telegram"}, "message": "m"}}) + "\n",
        encoding="utf-8")
    facets = compute_facets(tmp_path, "gateway")
    assert "ERROR" in facets["levels"]            # fixed enum
    assert "telegram" in facets["channels"]       # from newest segment


def test_telemetry_facets(tmp_path: Path):
    (tmp_path / "cli_default_2026-06-03.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "tg_42_2026-06-02.jsonl.gz").write_bytes(b"")
    facets = compute_facets(tmp_path, "telemetry")
    assert set(facets["sessions"]) == {"cli_default", "tg_42"}   # from filenames
    assert "memory.dream.end" in facets["types"]                 # from EVENTS registry
