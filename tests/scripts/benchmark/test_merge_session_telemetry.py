"""_merge_session_telemetry_into must not inherit earlier runs' events.

The session telemetry file is keyed by ``bench:<qa_id>`` + date, so it
accumulates across runs of the same QA on the same day. Pre-fix the
merge copied the whole file, so a re-run / replay / A-B run inherited
every earlier run's events — the 2026-06-10 CE-off run appeared to
carry cross-encoder rerank events that belonged to the morning
baseline. The merge now keeps only events stamped at/after the run's
wall-clock start (1s slack).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Under pytest, `tests/scripts/` IS the package named "scripts" (it has
# an __init__.py), so `scripts.benchmark` resolves to the TEST dir, not
# the repo's bench scripts. Follow test_locomo_judge's file-path
# loading — but locomo_harness imports its sibling locomo_dataset, so
# that sibling is pre-registered under its canonical dotted name (a
# plain sys.modules entry satisfies the import without touching the
# parent packages pytest owns).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BENCH_DIR = _REPO_ROOT / "scripts" / "benchmark"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


if "scripts.benchmark.locomo_dataset" not in sys.modules:
    _load_module(
        "scripts.benchmark.locomo_dataset", _BENCH_DIR / "locomo_dataset.py",
    )
_harness = _load_module(
    "scripts_benchmark_locomo_harness_under_test",
    _BENCH_DIR / "locomo_harness.py",
)
_merge_session_telemetry_into = _harness._merge_session_telemetry_into


def _event(ts: float, etype: str) -> str:
    return json.dumps({"ts": ts, "type": etype, "data": {}}) + "\n"


def test_merge_filters_events_before_run_start(tmp_path, monkeypatch):
    session_file = tmp_path / "bench_conv-0-q1.jsonl"
    session_file.write_text(
        _event(1000.0, "memory.recall.rerank")   # stale: earlier run
        + _event(5000.0, "memory.recall")          # fresh: this run
        + "not json\n"                             # malformed: dropped
        + _event(5001.0, "cache.usage"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "durin.telemetry.logger.get_session_logger",
        lambda key: SimpleNamespace(path=str(session_file)),
    )
    per_qa = tmp_path / "telemetry" / "conv-0-q1.jsonl"
    per_qa.parent.mkdir()
    per_qa.write_text(_event(4999.0, "context.composition"), encoding="utf-8")

    _merge_session_telemetry_into(per_qa, "conv-0-q1", started_wall=5000.0)

    lines = [json.loads(line) for line in per_qa.read_text().splitlines()]
    types = [e["type"] for e in lines]
    assert "memory.recall.rerank" not in types          # stale excluded
    assert "memory.recall" in types and "cache.usage" in types
    assert "context.composition" in types               # existing kept


def test_merge_noop_when_nothing_fresh(tmp_path, monkeypatch):
    session_file = tmp_path / "bench_conv-0-q2.jsonl"
    session_file.write_text(_event(1000.0, "memory.recall"), encoding="utf-8")
    monkeypatch.setattr(
        "durin.telemetry.logger.get_session_logger",
        lambda key: SimpleNamespace(path=str(session_file)),
    )
    per_qa = tmp_path / "conv-0-q2.jsonl"

    _merge_session_telemetry_into(per_qa, "conv-0-q2", started_wall=5000.0)

    assert not per_qa.exists()
