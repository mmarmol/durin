import json
from pathlib import Path

from durin.agent.skills_surface import quarantined_skills, skills_inventory


def test_quarantine_row_has_requirements(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".durin").mkdir()
    qdir = workspace / ".durin" / "import-quarantine" / "myskill"
    qdir.mkdir(parents=True)
    (qdir / "SKILL.md").write_text("---\nname: myskill\n---\nHello.\n")
    (qdir / ".scan.json").write_text(json.dumps({
        "source": "github:test/repo",
        "verdict": "safe",
        "findings": [],
        "requirements": {
            "platforms": {"value": ["macos"], "inferred": False},
            "bins": [{"name": "gh", "origin": "declared", "available": None}],
            "env": [],
            "compatibility": "",
            "installable": False,
            "blocked_by_platform": False,
            "platform_conflict": False,
        },
    }))
    rows = quarantined_skills(workspace)
    assert len(rows) == 1
    assert "requirements" in rows[0]
    assert rows[0]["requirements"]["bins"][0]["name"] == "gh"
    # Origins stripped in display model:
    assert "origin" not in rows[0]["requirements"]["bins"][0]


def test_inventory_row_has_requirements(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    skills_dir = workspace / "skills" / "myskill"
    skills_dir.mkdir(parents=True)
    prov = ("metadata:\n  durin:\n    provenance:\n"
            '      source: "github:o/r/x"\n      content_hash: "abc"\n'
            "    requirements:\n"
            "      platforms:\n"
            "        value: [macos]\n"
            "        inferred: false\n"
            "      bins:\n"
            "        - {name: gh, origin: declared, available: null}\n"
            "      env: []\n"
            "      compatibility: ''\n"
            "      installable: false\n"
            "      blocked_by_platform: false\n"
            "      platform_conflict: false\n")
    (skills_dir / "SKILL.md").write_text(
        f"---\nname: myskill\ndescription: d\n{prov}---\nHello.\n"
    )
    inv = {s["name"]: s for s in skills_inventory(workspace)}
    assert "myskill" in inv
    assert "requirements" in inv["myskill"]
    assert inv["myskill"]["requirements"]["bins"][0]["name"] == "gh"
