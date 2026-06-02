from durin.agent import skills_store as ss
from durin.agent.skills_frontmatter import split_frontmatter


def test_dream_create_skill_stamps_provenance_and_commits(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    res = ss.dream_create_skill(ws, "deploy-flow", "# Deploy\n\nSteps...\n", rationale="recurring deploy")
    assert res.get("ok") is True
    assert res.get("commit")
    text = (ws / "skills" / "deploy-flow" / "SKILL.md").read_text()
    data, body = split_frontmatter(text)
    durin = data["metadata"]["durin"]
    assert durin["mode"] == "auto"
    assert durin["provenance"]["source"] == "dream"
    assert "Deploy" in body


def test_dream_create_rejects_existing_skill(tmp_path):
    ws = tmp_path / "ws"
    (ws / "skills" / "x").mkdir(parents=True)
    (ws / "skills" / "x" / "SKILL.md").write_text("---\nname: x\n---\nbody\n")
    res = ss.dream_create_skill(ws, "x", "new", rationale="r")
    assert "error" in res


def test_dream_create_rejects_unsafe_name(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    res = ss.dream_create_skill(ws, "../escape", "c", rationale="r")
    assert "error" in res
