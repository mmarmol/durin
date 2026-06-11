from pathlib import Path

from durin.agent import skills_store as ss


def _make_builtin(tmp_path: Path) -> Path:
    b = tmp_path / "builtin"
    (b / "greet").mkdir(parents=True)
    (b / "greet" / "SKILL.md").write_text(
        "---\nname: greet\ndescription: say hi\n---\nBuiltin body\n", encoding="utf-8"
    )
    return b


def _make_user_skill(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: mine\n---\nBody\n", encoding="utf-8"
    )


def test_removable_action_classifies_three_cases(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(ss, "BUILTIN_SKILLS_DIR", _make_builtin(tmp_path))
    _make_user_skill(ws, "mine")          # workspace-only
    ss.fork_on_write(ws, "greet")         # fork the builtin into the workspace
    assert ss.removable_action(ws, "mine") == "remove"
    assert ss.removable_action(ws, "greet") == "revert"
    # pure builtin: no workspace copy
    monkeypatch.setattr(ss, "BUILTIN_SKILLS_DIR", _make_builtin(tmp_path / "b2"))
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    assert ss.removable_action(ws2, "greet") is None


def test_remove_workspace_skill_deletes_and_commits(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_user_skill(ws, "mine")
    res = ss.remove_skill(ws, "mine")
    assert res["ok"] is True
    assert res["action"] == "remove"
    assert res["commit"]  # a git sha was returned
    assert not (ws / "skills" / "mine").exists()


def test_remove_forked_builtin_reverts_to_builtin(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(ss, "BUILTIN_SKILLS_DIR", _make_builtin(tmp_path))
    ss.fork_on_write(ws, "greet")
    assert (ws / "skills" / "greet").exists()
    res = ss.remove_skill(ws, "greet")
    assert res["ok"] is True
    assert res["action"] == "revert"
    assert not (ws / "skills" / "greet").exists()   # workspace copy gone
    # builtin still resolvable through the loader
    assert "Builtin body" in (ss.read_skill_content(ws, "greet") or "")


def test_remove_pure_builtin_is_refused(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(ss, "BUILTIN_SKILLS_DIR", _make_builtin(tmp_path))
    res = ss.remove_skill(ws, "greet")
    assert "error" in res
    assert "builtin" in res["error"].lower()
    # the package builtin is untouched
    assert (ss.BUILTIN_SKILLS_DIR / "greet" / "SKILL.md").exists()


def test_remove_missing_skill_errors(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    res = ss.remove_skill(ws, "does-not-exist")
    assert "error" in res
    assert "not found" in res["error"].lower()


def test_remove_rejects_unsafe_name(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    res = ss.remove_skill(ws, "../escape")
    assert "error" in res


def test_web_skill_remove_status_codes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_user_skill(ws, "mine")
    status, payload = ss.web_skill_remove(ws, "mine")
    assert status == 200 and payload["ok"] is True
    status2, payload2 = ss.web_skill_remove(ws, "missing")
    assert status2 == 404 and "error" in payload2
