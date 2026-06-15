import json

from durin.agent.skills_frontmatter import split_frontmatter
from durin.agent.skills_import import install_imported_skill


def test_requirements_stamped_at_install(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "skills").mkdir()
    (workspace / ".durin").mkdir()

    qdir = tmp_path / "q" / "myskill"
    qdir.mkdir(parents=True)
    (qdir / "SKILL.md").write_text("---\nname: myskill\ndescription: A test skill.\n---\nHello.\n")
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

    result = install_imported_skill(workspace, qdir, source="github:test/repo",
                                    allowlist=[], confirmed=True)
    assert result["ok"] is True

    installed = workspace / "skills" / "myskill" / "SKILL.md"
    data, _ = split_frontmatter(installed.read_text())
    durin = data["metadata"]["durin"]
    assert "requirements" in durin
    assert durin["requirements"]["bins"][0]["name"] == "gh"
