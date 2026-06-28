import json
from pathlib import Path

from durin.agent import skill_curation as sc
from durin.agent import skill_suggestions as sg


def _manual_skill(ws: Path, name: str, body: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\ndurin:\n  mode: manual\n---\n{body}\n",
        encoding="utf-8")


def test_enqueues_suggestion_without_applying(tmp_path):
    ws = tmp_path
    _manual_skill(ws, "commit-helper", "## Verify\nold line\n")

    def judge(_prompt: str) -> str:
        return json.dumps({"actions": [
            {"type": "evolve", "name": "commit-helper",
             "old": "old line", "new": "new line", "rationale": "run tests first"}
        ]})

    out = sc.suggest_manual_skills(ws, judge=judge)
    assert out["suggested"] == 1
    sugg = sg.read_suggestions(ws)
    assert len(sugg) == 1 and sugg[0]["skill"] == "commit-helper"
    # NOT applied: the skill file still has the old content
    assert "old line" in (ws / "skills" / "commit-helper" / "SKILL.md").read_text()


def test_tombstoned_conclusion_is_suppressed(tmp_path):
    ws = tmp_path
    _manual_skill(ws, "x", "body\n")
    action = {"type": "evolve", "name": "x", "old": "body", "new": "better", "rationale": "r"}
    sg.add_tombstone(ws, sg.fingerprint(action))

    def judge(_p: str) -> str:
        return json.dumps({"actions": [action]})

    out = sc.suggest_manual_skills(ws, judge=judge)
    assert out["suggested"] == 0 and out["suppressed"] == 1
    assert sg.read_suggestions(ws) == []


def test_unchanged_skill_skipped_next_run(tmp_path):
    ws = tmp_path
    _manual_skill(ws, "x", "body\n")

    def judge(_p: str) -> str:
        return json.dumps({"actions": []})

    assert sc.suggest_manual_skills(ws, judge=judge)["reviewed"] == 1
    # second run: body unchanged -> nothing to review
    assert sc.suggest_manual_skills(ws, judge=judge)["reviewed"] == 0
