import json
from pathlib import Path

import durin.agent.skills_store as ss
from durin.security.skill_judge import JudgeOutcome


def _quarantined(tmp_path: Path) -> Path:
    q = tmp_path / ".durin" / "import-quarantine" / "demo"
    q.mkdir(parents=True)
    (q / "SKILL.md").write_text("---\nname: demo\ndescription: x\n---\nbody\n", encoding="utf-8")
    (q / ".scan.json").write_text(
        json.dumps({"source": "github:o/r", "verdict": "safe", "findings": []}),
        encoding="utf-8",
    )
    return tmp_path


def test_web_judge_returns_and_persists_summary(tmp_path, monkeypatch):
    ws = _quarantined(tmp_path)
    monkeypatch.setattr(ss, "_import_judge", lambda: ("always", "m", "caution"))
    monkeypatch.setattr(
        "durin.security.skill_judge.judge_skill",
        lambda *a, **k: JudgeOutcome(findings=[], verdict="safe", summary="Reviewed instructions; clean."),
    )
    status, payload = ss.web_skill_judge(ws, "demo")
    assert status == 200 and payload["judged"] is True
    assert payload["summary"] == "Reviewed instructions; clean."
    stored = json.loads((ws / ".durin" / "import-quarantine" / "demo" / ".scan.json").read_text())
    assert stored["summary"] == "Reviewed instructions; clean."


def test_web_judge_unreachable_error_code(tmp_path, monkeypatch):
    ws = _quarantined(tmp_path)
    monkeypatch.setattr(ss, "_import_judge", lambda: ("always", "m", "caution"))

    def boom(*a, **k):
        raise Exception("litellm.InternalServerError: OpenAIException - Connection error.")

    monkeypatch.setattr("durin.security.skill_judge.judge_skill", boom)
    status, payload = ss.web_skill_judge(ws, "demo")
    assert status == 200 and payload["judged"] is False
    assert payload["error_code"] == "unreachable"
