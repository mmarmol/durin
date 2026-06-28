from pathlib import Path

from durin.utils.gitstore import GitStore


def _commit_file(store: GitStore, root: Path, rel: str, content: str, msg: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return store.auto_commit(msg)


def test_commit_diff_scoped_to_path(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    store = GitStore(root, subtree=True, label="skills")
    store.init()
    _commit_file(store, root, "alpha/SKILL.md", "alpha v1\n", "add alpha")
    # One commit touches BOTH alpha and beta
    (root / "alpha" / "SKILL.md").write_text("alpha v2\n", encoding="utf-8")
    (root / "beta" / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (root / "beta" / "SKILL.md").write_text("beta v1\n", encoding="utf-8")
    sha = store.auto_commit("touch alpha and beta")

    res = store.commit_diff(sha, path="alpha")
    assert res is not None
    info, patch = res
    assert "alpha/SKILL.md" in patch
    assert "alpha v2" in patch
    # beta's change must NOT appear in alpha's scoped diff
    assert "beta/SKILL.md" not in patch
    assert "beta v1" not in patch


def test_commit_diff_unknown_sha(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    store = GitStore(root, subtree=True, label="skills")
    store.init()
    _commit_file(store, root, "alpha/SKILL.md", "x\n", "add")
    assert store.commit_diff("deadbeef", path="alpha") is None
