import json
from pathlib import Path

from durin.agent.skills_store import Attribution, dream_create_skill
from durin.telemetry.logger import TelemetryLogger, bind_telemetry, reset_telemetry


def _events(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l] if p.exists() else []


def test_dream_create_emits_skill_authored_write_ramp(tmp_path):
    log = tmp_path / "t.jsonl"
    token = bind_telemetry(TelemetryLogger(log))
    try:
        out = dream_create_skill(
            tmp_path, "notes", "---\nname: notes\ndescription: fold notes. use when asked.\n---\nbody\n",
            "recurring", attribution=Attribution(actor="agent", session="s1", agent="m1"))
    finally:
        reset_telemetry(token)
    assert out.get("ok"), out
    authored = [e for e in _events(log) if e["type"] == "skill.authored"]
    assert len(authored) == 1
    d = authored[0]["data"]
    assert d["name"] == "notes" and d["ramp"] == "write" and d["actor"] == "agent"
    assert d["composition"] == "compliant" and d["files_count"] == 0


def test_quarantine_emits_no_authored_event(tmp_path):
    log = tmp_path / "t.jsonl"
    body = "---\nname: risky\ndescription: do a thing. use when asked.\n---\nrun scripts/x.py\n"
    token = bind_telemetry(TelemetryLogger(log))
    try:
        out = dream_create_skill(tmp_path, "risky", body, "r",
                                 files={"scripts/x.py": "import os\nos.environ['SECRET']\n"})
    finally:
        reset_telemetry(token)
    assert out.get("quarantined") is True
    assert [e for e in _events(log) if e["type"] == "skill.authored"] == []
