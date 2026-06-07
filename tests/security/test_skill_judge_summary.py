from pathlib import Path

from durin.security import skill_judge


def _write_skill(tmp_path: Path) -> Path:
    d = tmp_path / "demo"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: demo\ndescription: x\n---\nbody\n", encoding="utf-8"
    )
    return d


def test_judge_parses_summary_verdict_findings(tmp_path):
    raw = (
        "===SUMMARY===\nChecked SKILL.md and scripts for injection and exfiltration; none found.\n"
        "===VERDICT===\nsafe\n"
        "===FINDINGS===\nnone\n===END===\n"
    )
    d = _write_skill(tmp_path)
    out = skill_judge.judge_skill(
        d, llm_invoke=lambda *a, **k: skill_judge.LLMResponseText(raw), model="x"
    )
    assert out.verdict == "safe"
    assert out.findings == []
    assert "exfiltration" in out.summary


def test_judge_parses_findings_and_caution(tmp_path):
    raw = (
        "===SUMMARY===\nFound a curl|bash installer.\n"
        "===VERDICT===\ncaution\n"
        "===FINDINGS===\ncaution | dangerous_code | scripts/go.sh | fetch-and-execute\n===END===\n"
    )
    d = _write_skill(tmp_path)
    out = skill_judge.judge_skill(
        d, llm_invoke=lambda *a, **k: skill_judge.LLMResponseText(raw), model="x"
    )
    assert out.verdict == "caution"
    assert len(out.findings) == 1
    assert out.findings[0].category == "llm:dangerous_code"
    assert out.summary.startswith("Found")


def test_missing_summary_is_tolerated(tmp_path):
    raw = "===FINDINGS===\nnone\n===END===\n"
    d = _write_skill(tmp_path)
    out = skill_judge.judge_skill(
        d, llm_invoke=lambda *a, **k: skill_judge.LLMResponseText(raw), model="x"
    )
    assert out.findings == []
    assert out.summary == ""
