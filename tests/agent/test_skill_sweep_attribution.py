import json

from durin.agent.skill_lifecycle import sweep_unverified_skills
from durin.agent.skills_store import _store_init
from durin.telemetry.logger import TelemetryLogger, bind_telemetry, reset_telemetry


def _raw_skill(ws, name):
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: demo. use when asked.\n---\nbody\n",
                                encoding="utf-8")


def _events(p) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l] if p.exists() else []


def test_attributed_when_commit_has_session(tmp_path):
    _raw_skill(tmp_path, "rogue")
    store = _store_init(tmp_path)
    store.auto_commit("skill(rogue): draft landed", trailers={"Actor": "agent", "Session": "sess-9"})
    moved = sweep_unverified_skills(tmp_path)
    assert moved == ["rogue"]
    scan = json.loads((tmp_path / ".durin" / "import-quarantine" / "rogue" / ".scan.json").read_text())
    assert scan["source"] == "agent:session:sess-9"


def test_falls_back_to_unverified(tmp_path):
    _raw_skill(tmp_path, "orphan")   # no commit / no Session trailer
    sweep_unverified_skills(tmp_path)
    scan = json.loads((tmp_path / ".durin" / "import-quarantine" / "orphan" / ".scan.json").read_text())
    assert scan["source"] == "unverified:workspace"


def test_attributed_sweep_emits_skill_authored_backstop(tmp_path):
    _raw_skill(tmp_path, "rogue")
    store = _store_init(tmp_path)
    store.auto_commit("skill(rogue): draft landed", trailers={"Actor": "agent", "Session": "sess-9"})
    log = tmp_path / "t.jsonl"
    token = bind_telemetry(TelemetryLogger(log))
    try:
        sweep_unverified_skills(tmp_path)
    finally:
        reset_telemetry(token)
    authored = [e for e in _events(log) if e["type"] == "skill.authored"]
    assert len(authored) == 1
    d = authored[0]["data"]
    assert d["name"] == "rogue"
    assert d["actor"] == "agent"
    assert d["session"] == "sess-9"
    assert d["model"] is None
    assert d["ramp"] == "backstop"
    assert d["composition"] == "compliant"
    assert d["scan_verdict"] in ("caution", "dangerous")
    assert d["files_count"] == 0


def test_unattributed_sweep_emits_no_skill_authored(tmp_path):
    _raw_skill(tmp_path, "orphan")
    log = tmp_path / "t.jsonl"
    token = bind_telemetry(TelemetryLogger(log))
    try:
        sweep_unverified_skills(tmp_path)
    finally:
        reset_telemetry(token)
    assert [e for e in _events(log) if e["type"] == "skill.authored"] == []
