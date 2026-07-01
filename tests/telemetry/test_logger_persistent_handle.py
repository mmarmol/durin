from pathlib import Path

from durin.telemetry import logger as tlog


def test_writes_are_immediately_readable(tmp_path):
    tlog.close_all_handles()
    tl = tlog.TelemetryLogger(tmp_path / "s_2026-01-01.jsonl", session_key="s")
    tl.log("e1", {"a": 1})
    tl.log("e2", {"b": 2})
    lines = tl.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"type":"e1"' in lines[0]
    assert '"type":"e2"' in lines[1]


def test_handle_reused_not_reopened_per_event(tmp_path, monkeypatch):
    tlog.close_all_handles()
    path = tmp_path / "reuse_2026-01-01.jsonl"
    tl = tlog.TelemetryLogger(path, session_key="reuse")
    opens = {"n": 0}
    real_open = Path.open

    def counting_open(self, *a, **k):
        if self == path:
            opens["n"] += 1
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", counting_open)
    tl.log("e1")
    tl.log("e2")
    tl.log("e3")
    assert opens["n"] == 1


def test_lru_eviction_and_close_all(tmp_path):
    tlog.close_all_handles()
    cap = tlog._MAX_OPEN_HANDLES
    for i in range(cap + 5):
        tl = tlog.TelemetryLogger(tmp_path / f"s{i}_2026-01-01.jsonl", session_key=f"s{i}")
        tl.log("e", {"i": i})
    assert len(tlog._open_handles) <= cap
    tlog.close_all_handles()
    assert len(tlog._open_handles) == 0
