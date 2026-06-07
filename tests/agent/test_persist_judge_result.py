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
