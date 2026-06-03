import json

from durin.logs.reader import parse_line, session_from_filename


def test_parse_gateway_loguru_jsonl():
    raw = json.dumps({
        "text": "...",
        "record": {
            "time": {"timestamp": 1717430000.5},
            "level": {"name": "ERROR"},
            "extra": {"channel": "telegram"},
            "message": "boom",
        },
    })
    line = parse_line("gateway", raw, session=None)
    assert line is not None
    assert line.ts == 1717430000.5
    assert line.fields["level"] == "ERROR"
    assert line.fields["channel"] == "telegram"
    assert line.fields["message"] == "boom"


def test_parse_telemetry_jsonl():
    raw = json.dumps({"ts": 1717430001.0, "type": "memory.dream_start", "data": {"k": 1}})
    line = parse_line("telemetry", raw, session="cli_default")
    assert line.ts == 1717430001.0
    assert line.fields["type"] == "memory.dream_start"
    assert line.fields["session"] == "cli_default"


def test_parse_bad_line_returns_none():
    assert parse_line("gateway", "not json", session=None) is None
    assert parse_line("gateway", "", session=None) is None


def test_session_from_filename():
    assert session_from_filename("cli_default_2026-06-03.jsonl") == "cli_default"
    assert session_from_filename("tg_42_2026-06-02.jsonl.gz") == "tg_42"
    assert session_from_filename("weird.txt") == "weird.txt"
