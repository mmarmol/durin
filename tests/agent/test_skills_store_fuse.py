from durin.agent import skills_store as ss


def _mk(ws, name):
    d = ws / "skills" / name; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody {name}\n", encoding="utf-8")


def test_fuse_writes_c_removes_sources(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a"); _mk(ws, "git-b")
    res = ss.dream_fuse_skills(
        ws, target="git-flow", content="# Git flow\n\nmerged\n",
        sources=["git-a", "git-b"], rationale="overlap")
    assert res.get("ok") is True
    assert (ws / "skills" / "git-flow" / "SKILL.md").exists()
    assert not (ws / "skills" / "git-a").exists()
    assert not (ws / "skills" / "git-b").exists()


def test_fuse_refuses_manual_source(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a")
    d = ws / "skills" / "mine"; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: mine\nmetadata:\n  durin:\n    mode: manual\n---\nx\n", encoding="utf-8")
    res = ss.dream_fuse_skills(ws, target="c", content="x",
                               sources=["git-a", "mine"], rationale="r")
    assert "error" in res
    assert (ws / "skills" / "git-a").exists()  # nothing removed on refusal


def test_fuse_refuses_existing_target(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a"); _mk(ws, "taken")
    res = ss.dream_fuse_skills(ws, target="taken", content="x",
                               sources=["git-a"], rationale="r")
    assert "error" in res
