import re
from pathlib import Path

import pytest

from durin.security import skill_judge

_TOKEN_RE = re.compile(r"^([0-9a-f]{16})$", re.MULTILINE)


def _end_token(prompt: str) -> str:
    m = _TOKEN_RE.search(prompt)
    return m.group(1) if m else ""


def _write_skill(tmp_path: Path) -> Path:
    d = tmp_path / "demo"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: demo\ndescription: x\n---\nbody\n", encoding="utf-8"
    )
    return d


def test_judge_parses_summary_verdict_findings(tmp_path):
    def _invoke(prompt, **_):
        return skill_judge.LLMResponseText(
            "===SUMMARY===\nChecked SKILL.md and scripts for injection and exfiltration; none found.\n"
            "===VERDICT===\nsafe\n"
            f"===FINDINGS===\nnone\n===TOOLS===\nnone\n===END {_end_token(prompt)}===\n")

    d = _write_skill(tmp_path)
    out = skill_judge.judge_skill(d, llm_invoke=_invoke, model="x")
    assert out.verdict == "safe"
    assert out.findings == []
    assert "exfiltration" in out.summary


def test_judge_parses_findings_and_caution(tmp_path):
    def _invoke(prompt, **_):
        return skill_judge.LLMResponseText(
            "===SUMMARY===\nFound a curl|bash installer.\n"
            "===VERDICT===\ncaution\n"
            "===FINDINGS===\ncaution | dangerous_code | scripts/go.sh | fetch-and-execute\n"
            f"===TOOLS===\nnone\n===END {_end_token(prompt)}===\n")

    d = _write_skill(tmp_path)
    out = skill_judge.judge_skill(d, llm_invoke=_invoke, model="x")
    assert out.verdict == "caution"
    assert len(out.findings) == 1
    assert out.findings[0].category == "llm:dangerous_code"
    assert out.summary.startswith("Found")


def test_missing_summary_raises(tmp_path):
    # A reply missing any of the five required markers is no longer tolerated
    # with a blank default: it fails the parse (fail-safe — the caller degrades
    # to the deterministic scan or asks a person), since silently accepting a
    # partial reply is exactly what let a spoofed reply through before.
    raw = "===FINDINGS===\nnone\n===TOOLS===\nnone\n===END===\n"
    d = _write_skill(tmp_path)
    with pytest.raises(skill_judge.JudgeError):
        skill_judge.judge_skill(
            d, llm_invoke=lambda *a, **k: skill_judge.LLMResponseText(raw), model="x",
            max_retries=0,
        )
