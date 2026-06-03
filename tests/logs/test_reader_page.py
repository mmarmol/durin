import gzip
import json
import os
from pathlib import Path

from durin.logs.reader import LogQuery, read_page


def _write_gateway(path: Path, rows):
    lines = []
    for ts, level, channel, msg in rows:
        lines.append(json.dumps({"record": {
            "time": {"timestamp": ts}, "level": {"name": level},
            "extra": {"channel": channel}, "message": msg,
        }}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_newest_first_and_limit(tmp_path: Path):
    _write_gateway(tmp_path / "gateway.log", [
        (100.0, "INFO", "a", "old"),
        (200.0, "INFO", "a", "mid"),
        (300.0, "ERROR", "b", "new"),
    ])
    page = read_page(tmp_path, LogQuery(source="gateway", limit=2, window_hours=None))
    assert [l["fields"]["message"] for l in page.lines] == ["new", "mid"]
    assert page.has_more is True
    assert page.next_cursor == 200.0


def test_cursor_resumes(tmp_path: Path):
    _write_gateway(tmp_path / "gateway.log", [
        (100.0, "INFO", "a", "old"),
        (200.0, "INFO", "a", "mid"),
        (300.0, "ERROR", "b", "new"),
    ])
    page = read_page(tmp_path, LogQuery(source="gateway", limit=2,
                                        before_ts=200.0, window_hours=None))
    assert [l["fields"]["message"] for l in page.lines] == ["old"]
    assert page.has_more is False


def test_grep_and_level_filter(tmp_path: Path):
    _write_gateway(tmp_path / "gateway.log", [
        (100.0, "INFO", "a", "keep me"),
        (200.0, "ERROR", "b", "ignore"),
        (300.0, "ERROR", "b", "keep me too"),
    ])
    page = read_page(tmp_path, LogQuery(source="gateway", q="keep",
                                        filters={"level": {"ERROR"}}, window_hours=None))
    assert [l["fields"]["message"] for l in page.lines] == ["keep me too"]


def test_reads_gz_segments(tmp_path: Path):
    plain = tmp_path / "gateway.log"
    _write_gateway(plain, [(300.0, "INFO", "a", "active")])
    gz = tmp_path / "gateway.2.log.gz"
    payload = json.dumps({"record": {"time": {"timestamp": 100.0},
                                     "level": {"name": "INFO"}, "extra": {"channel": "a"},
                                     "message": "archived"}}) + "\n"
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write(payload)
    # Rotated segments are older than the active file (mtime models reality).
    os.utime(gz, (100.0, 100.0))
    os.utime(plain, (300.0, 300.0))
    page = read_page(tmp_path, LogQuery(source="gateway", limit=10, window_hours=None))
    assert [l["fields"]["message"] for l in page.lines] == ["active", "archived"]


def test_excludes_boot_log(tmp_path: Path):
    _write_gateway(tmp_path / "gateway.log", [(300.0, "INFO", "a", "real")])
    # boot.log is raw text, not JSONL — must be ignored by the gateway reader.
    (tmp_path / "gateway.boot.log").write_text("Traceback (most recent call last):\n", encoding="utf-8")
    page = read_page(tmp_path, LogQuery(source="gateway", limit=10, window_hours=None))
    assert [l["fields"]["message"] for l in page.lines] == ["real"]
