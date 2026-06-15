import json

import durin.agent.skills_store as ss


def test_persist_writes_scan_json(tmp_path):
    q = tmp_path / "demo"
    q.mkdir()
    (q / ".scan.json").write_text(
        json.dumps({"source": "github:o/r", "verdict": "safe", "findings": []}), encoding="utf-8"
    )
    ss._persist_judge_result(
        q, "github:o/r", "caution",
        [{"category": "llm:x", "severity": "caution", "where": "SKILL.md", "detail": "y"}],
        "Found one issue.",
    )
    stored = json.loads((q / ".scan.json").read_text())
    assert stored["verdict"] == "caution"
    assert stored["summary"] == "Found one issue."
    assert stored["findings"][0]["category"] == "llm:x"


def test_persist_preserves_requirements_key(tmp_path):
    """Re-running the judge must not destroy the `requirements` manifest that
    `fetch_candidate` wrote — it should merge, not overwrite."""
    q = tmp_path / "demo"
    q.mkdir()
    (q / ".scan.json").write_text(
        json.dumps({
            "source": "github:o/r",
            "verdict": "safe",
            "findings": [],
            "requirements": {"bins": [{"name": "gh"}], "platforms": {"value": ["macos"]}},
        }),
        encoding="utf-8",
    )
    ss._persist_judge_result(
        q, "github:o/r", "caution",
        [{"category": "llm:x", "severity": "caution", "where": "SKILL.md", "detail": "y"}],
        "Found one issue.",
    )
    stored = json.loads((q / ".scan.json").read_text())
    assert stored["verdict"] == "caution"
    assert "requirements" in stored
    assert stored["requirements"]["bins"][0]["name"] == "gh"
