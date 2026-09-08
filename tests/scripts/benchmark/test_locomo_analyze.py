from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PATH = _REPO_ROOT / "scripts" / "benchmark" / "locomo_analyze.py"
_spec = importlib.util.spec_from_file_location("locomo_analyze", _PATH)
analyze = importlib.util.module_from_spec(_spec)
sys.modules["locomo_analyze"] = analyze  # dataclass needs it discoverable during exec
_spec.loader.exec_module(analyze)


def _trace() -> dict:
    return {"stop_reason": "stop", "category": "single_hop", "tool_calls": [],
            "expected": "blue", "got": "red"}


def test_no_tool_calls_and_no_prefetch_is_no_retrieval():
    tel = analyze._Telemetry(events=[])
    assert analyze._classify_failure(_trace(), {"confidence": 90}, tel) == "no_retrieval"


def test_prefetch_hits_count_as_retrieval():
    tel = analyze._Telemetry(events=[{"type": "memory.prefetch", "data": {"hits": 2}}])
    assert analyze._classify_failure(_trace(), {"confidence": 90}, tel) != "no_retrieval"
