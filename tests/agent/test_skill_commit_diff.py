from pathlib import Path

from durin.agent import skills_store as ss


def _seed_manual_skill(ws: Path, name: str, body: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\ndurin:\n  mode: manual\n---\n{body}\n",
        encoding="utf-8")


def test_web_commit_diff_returns_scoped_patch(tmp_path):
    ws = tmp_path
    _seed_manual_skill(ws, "alpha", "v1")
    store = ss._store_init(ws)  # ensure the skills git store exists
    sha1 = store.auto_commit("seed alpha")
    (ws / "skills" / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: d\ndurin:\n  mode: manual\n---\nv2\n",
        encoding="utf-8")
    sha2 = store.auto_commit("edit alpha")

    status, payload = ss.web_commit_diff(ws, "alpha", sha2)
    assert status == 200
    assert "alpha/SKILL.md" in payload["patch"]
    assert "v2" in payload["patch"]

    status_bad, _ = ss.web_commit_diff(ws, "alpha", "deadbeef")
    assert status_bad == 404
