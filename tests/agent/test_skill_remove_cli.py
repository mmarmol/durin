from pathlib import Path

from typer.testing import CliRunner

from durin.cli.skill_cmd import skill_app

runner = CliRunner()


def _make_user_skill(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: mine\n---\nBody\n", encoding="utf-8")


def test_cli_remove_with_yes(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_user_skill(ws, "mine")
    monkeypatch.setattr("durin.cli.skill_cmd._workspace_root", lambda: ws)
    result = runner.invoke(skill_app, ["remove", "mine", "--yes"])
    assert result.exit_code == 0, result.output
    assert "removed" in result.output.lower()
    assert not (ws / "skills" / "mine").exists()


def test_cli_remove_missing_errors(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr("durin.cli.skill_cmd._workspace_root", lambda: ws)
    result = runner.invoke(skill_app, ["remove", "ghost", "--yes"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()
