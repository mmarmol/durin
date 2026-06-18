import json
from pathlib import Path

from durin.logs.reader import LogQuery, read_page


def test_read_page_end_to_end(tmp_path: Path):
    (tmp_path / "gateway.log").write_text(
        json.dumps({"record": {"time": {"timestamp": 5.0}, "level": {"name": "INFO"},
                               "extra": {"channel": "a"}, "message": "hi"}}) + "\n",
        encoding="utf-8")
    page = read_page(tmp_path, LogQuery(source="gateway", window_hours=None))
    assert page.lines[0]["fields"]["message"] == "hi"
