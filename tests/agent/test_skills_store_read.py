from pathlib import Path

from durin.agent import skills_store as ss


def _make_builtin(tmp_path: Path) -> Path:
    b = tmp_path / "builtin"
    (b / "greet").mkdir(parents=True)
    (b / "greet" / "SKILL.md").write_text(
        "---\nname: greet\ndescription: say hi\n---\nBody\n", encoding="utf-8"
    )
    return b


def _make_user_skill(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: mine\n---\nBody\n", encoding="utf-8"
    )


def test_read_mode_defaults_builtin_auto_user_manual(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(ss, "BUILTIN_SKILLS_DIR", _make_builtin(tmp_path))
    _make_user_skill(ws, "mine")
    assert ss.read_mode(ws, "greet") == "auto"
    assert ss.read_mode(ws, "mine") == "manual"


def test_fork_on_write_copies_builtin_and_stamps_provenance(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    builtin = _make_builtin(tmp_path)
    monkeypatch.setattr(ss, "BUILTIN_SKILLS_DIR", builtin)
    dest = ss.fork_on_write(ws, "greet")
    assert (dest / "SKILL.md").exists()
    assert (builtin / "greet" / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert ss.read_mode(ws, "greet") == "auto"
    info = {s["name"]: s for s in ss.list_skills_info(ws)}
    assert info["greet"]["provenance"]["source"] == "builtin:greet"


def test_list_skills_info_reports_source_and_mode(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(ss, "BUILTIN_SKILLS_DIR", _make_builtin(tmp_path))
    _make_user_skill(ws, "mine")
    by_name = {s["name"]: s for s in ss.list_skills_info(ws)}
    assert by_name["greet"]["source"] == "builtin"
    assert by_name["greet"]["mode"] == "auto"
    assert by_name["mine"]["source"] == "workspace"
    assert by_name["mine"]["mode"] == "manual"
