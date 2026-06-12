from pathlib import Path

from durin.utils.gitstore import GitStore, _compose_with_trailers


def test_compose_with_trailers_appends_block():
    msg = _compose_with_trailers("skill(foo): edited SKILL.md via web",
                                 {"Actor": "user", "Session": "abc123"})
    assert msg.splitlines()[0] == "skill(foo): edited SKILL.md via web"
    assert "Actor: user" in msg
    assert "Session: abc123" in msg
    # subject and trailers separated by a blank line
    assert "\n\nActor:" in msg or "\n\nSession:" in msg


def test_compose_with_trailers_none_is_bare_subject():
    assert _compose_with_trailers("subject only", None) == "subject only"
    assert _compose_with_trailers("subject only", {}) == "subject only"


def test_auto_commit_writes_trailers_into_log(tmp_path: Path):
    store = GitStore(tmp_path, subtree=True, label="skills")
    store.init()
    (tmp_path / "foo").mkdir()
    (tmp_path / "foo" / "SKILL.md").write_text("hi", encoding="utf-8")
    sha = store.auto_commit("skill(foo): create", trailers={"Actor": "agent", "Agent": "claude-opus-4-8"})
    assert sha
    msg = store.log(max_entries=1)[0].message
    assert "skill(foo): create" in msg
    assert "Actor: agent" in msg
    assert "Agent: claude-opus-4-8" in msg


def test_log_path_filters_to_one_skill(tmp_path: Path):
    store = GitStore(tmp_path, subtree=True, label="skills")
    store.init()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "SKILL.md").write_text("a1", encoding="utf-8")
    store.auto_commit("skill(alpha): create")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "SKILL.md").write_text("b1", encoding="utf-8")
    store.auto_commit("skill(beta): create")
    (tmp_path / "alpha" / "SKILL.md").write_text("a2", encoding="utf-8")
    store.auto_commit("skill(alpha): edit")

    alpha = store.log(path="alpha")
    subjects = [c.message.splitlines()[0] for c in alpha]
    assert subjects == ["skill(alpha): edit", "skill(alpha): create"]
    # beta's commit must not appear in alpha's history
    assert all("beta" not in s for s in subjects)
