import json
from pathlib import Path

from durin.logs.reader import LogQuery, read_page


def test_query_from_params_builds_filters():
    from durin.channels.websocket import _logs_query_from_params
    q = _logs_query_from_params({
        "source": ["telemetry"], "q": ["dream"], "type": ["memory.dream.end"],
        "before_ts": ["1717430000.0"], "window_hours": ["48"], "limit": ["50"],
    })
    assert q.source == "telemetry"
    assert q.q == "dream"
    assert q.before_ts == 1717430000.0
    assert q.window_hours == 48.0
    assert q.limit == 50
    assert q.filters["type"] == {"memory.dream.end"}


def test_query_from_params_defaults_and_window_all():
    from durin.channels.websocket import _logs_query_from_params
    q = _logs_query_from_params({"window_hours": ["all"]})
    assert q.source == "gateway"
    assert q.window_hours is None
    assert q.limit == 200


def test_read_page_end_to_end(tmp_path: Path):
    (tmp_path / "gateway.log").write_text(
        json.dumps({"record": {"time": {"timestamp": 5.0}, "level": {"name": "INFO"},
                               "extra": {"channel": "a"}, "message": "hi"}}) + "\n",
        encoding="utf-8")
    page = read_page(tmp_path, LogQuery(source="gateway", window_hours=None))
    assert page.lines[0]["fields"]["message"] == "hi"
