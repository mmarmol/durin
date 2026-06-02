from pathlib import Path

from durin.agent import skills_store as ss


def _user_skill(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\nBody\n", encoding="utf-8"
    )


def test_web_list_returns_200_and_skills(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    status, payload = ss.web_list(ws)
    assert status == 200
    assert any(s["name"] == "mine" for s in payload["skills"])


def test_web_get_returns_content_or_404(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    status, payload = ss.web_get(ws, "mine")
    assert status == 200 and "Body" in payload["content"]
    assert ss.web_get(ws, "nope")[0] == 404


def test_web_mode_sets_and_validates(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    assert ss.web_mode(ws, "mine", "auto")[0] == 200
    assert ss.read_mode(ws, "mine") == "auto"
    assert ss.web_mode(ws, "mine", "bogus")[0] == 400


def test_web_save_requires_manual(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    assert ss.web_save(ws, "mine", "---\nname: mine\ndescription: d\n---\nNEW\n")[0] == 200
    ss.web_mode(ws, "mine", "auto")
    assert ss.web_save(ws, "mine", "x")[0] == 400
