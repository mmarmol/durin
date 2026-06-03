import gzip
import json
from pathlib import Path

from loguru import logger

from durin.cli.gateway_logging import configure_gateway_file_logging


def test_file_sink_writes_jsonl(tmp_path: Path):
    log_file = tmp_path / "gateway.log"
    sink_id = configure_gateway_file_logging(log_file, max_file_mb=5, retention_days=7)
    try:
        logger.bind(channel="test").info("hello world")
    finally:
        logger.remove(sink_id)
    line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    obj = json.loads(line)
    assert obj["record"]["message"] == "hello world"
    assert obj["record"]["level"]["name"] == "INFO"
    assert obj["record"]["extra"]["channel"] == "test"


def test_rotation_by_size_and_gz(tmp_path: Path):
    log_file = tmp_path / "gateway.log"
    # 1 MB threshold so the test rotates quickly.
    sink_id = configure_gateway_file_logging(log_file, max_file_mb=1, retention_days=7)
    try:
        for i in range(8000):
            logger.bind(channel="bulk").info("x" * 200 + f" {i}")
    finally:
        logger.remove(sink_id)
    rotated = list(tmp_path.glob("*.gz"))
    assert rotated, f"expected gz-compressed rotated segment, dir had: {[p.name for p in tmp_path.iterdir()]}"
    with gzip.open(rotated[0], "rt", encoding="utf-8") as fh:
        first = json.loads(fh.readline())
    assert "record" in first
