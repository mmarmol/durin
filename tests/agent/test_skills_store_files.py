from pathlib import Path

from durin.agent.skills_store import skill_files, read_skill_file, save_skill_file


def _mk_skill(ws: Path, name: str) -> Path:
    d = ws / "skills" / name
    (d / "references").mkdir(parents=True)
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: x\n---\nbody\n", encoding="utf-8")
    (d / "references" / "notes.md").write_text("notes", encoding="utf-8")
    (d / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
    (d / "logo.png").write_bytes(b"\x89PNG\x00\x01\x02\x03")
    return d


def test_skill_files_lists_nested_and_flags_binary(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    files = {f["path"]: f for f in skill_files(tmp_path, "demo")}
    assert set(files) == {"SKILL.md", "references/notes.md", "scripts/run.py", "logo.png"}
    assert files["scripts/run.py"]["text"] is True
    assert files["logo.png"]["text"] is False
    assert files["SKILL.md"]["size"] > 0


def test_skill_files_unknown_skill_is_empty(tmp_path: Path):
    assert skill_files(tmp_path, "nope") == []


def test_skill_files_rejects_unsafe_name(tmp_path: Path):
    assert skill_files(tmp_path, "../etc") == []


def test_read_skill_file_text_and_binary(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    md = read_skill_file(tmp_path, "demo", "references/notes.md")
    assert md == {"path": "references/notes.md", "text": True, "content": "notes"}
    png = read_skill_file(tmp_path, "demo", "logo.png")
    assert png["text"] is False and png["content"] == ""


def test_read_skill_file_rejects_traversal(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    assert read_skill_file(tmp_path, "demo", "../demo/SKILL.md") is None
    assert read_skill_file(tmp_path, "demo", "/etc/passwd") is None


def test_read_skill_file_missing_file(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    assert read_skill_file(tmp_path, "demo", "nope.md") is None


from durin.agent.skills_store import set_mode, Attribution


def test_save_skill_file_rejects_directory_path(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    set_mode(tmp_path, "demo", "manual")
    res = save_skill_file(tmp_path, "demo", "references", "x")
    assert "error" in res


def test_save_skill_file_refuses_auto(tmp_path: Path):
    _mk_skill(tmp_path, "demo")  # workspace skill defaults to manual; force auto
    set_mode(tmp_path, "demo", "auto")
    res = save_skill_file(tmp_path, "demo", "references/notes.md", "new", rationale="r")
    assert "error" in res and "manual" in res["error"]


def test_save_skill_file_writes_commits_and_rescans(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    set_mode(tmp_path, "demo", "manual")
    res = save_skill_file(tmp_path, "demo", "references/notes.md", "updated body",
                          rationale="edited references/notes.md via web",
                          attribution=Attribution(actor="user"))
    assert res["ok"] is True and res["commit"]
    assert "verdict" in res  # security re-scan included (non-blocking)
    assert (tmp_path / "skills" / "demo" / "references" / "notes.md").read_text() == "updated body"


def test_save_skill_file_blocks_python_syntax_error(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    set_mode(tmp_path, "demo", "manual")
    before = (tmp_path / "skills" / "demo" / "scripts" / "run.py").read_text()
    res = save_skill_file(tmp_path, "demo", "scripts/run.py", "def (oops\n", rationale="r")
    assert res.get("error") == "syntax" and res.get("lang") == "python"
    assert isinstance(res.get("line"), int)
    # NOT written
    assert (tmp_path / "skills" / "demo" / "scripts" / "run.py").read_text() == before


def test_save_skill_file_accepts_valid_python(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    set_mode(tmp_path, "demo", "manual")
    res = save_skill_file(tmp_path, "demo", "scripts/run.py", "print(2)\n", rationale="r")
    assert res["ok"] is True


from durin.agent.skills_store import web_files, web_file_get, web_file_save, web_history


def test_web_files_and_file_get(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    status, payload = web_files(tmp_path, "demo")
    assert status == 200 and any(f["path"] == "scripts/run.py" for f in payload["files"])
    status, payload = web_file_get(tmp_path, "demo", "references/notes.md")
    assert status == 200 and payload["content"] == "notes"
    status, payload = web_file_get(tmp_path, "demo", "../escape")
    assert status == 404


def test_web_file_save_manual_and_syntax(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    set_mode(tmp_path, "demo", "manual")
    status, payload = web_file_save(tmp_path, "demo", "references/notes.md", "x",
                                    attribution=Attribution(actor="user"))
    assert status == 200 and payload["ok"] is True
    status, payload = web_file_save(tmp_path, "demo", "scripts/run.py", "def (\n",
                                    attribution=Attribution(actor="user"))
    assert status == 400 and payload["error"] == "syntax"


def test_web_history_shape(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    set_mode(tmp_path, "demo", "manual")
    status, payload = web_history(tmp_path, "demo")
    assert status == 200 and "provenance" in payload and isinstance(payload["commits"], list)


def test_skill_files_skips_pycache(tmp_path: Path):
    _mk_skill(tmp_path, "demo")
    pyc_dir = tmp_path / "skills" / "demo" / "scripts" / "__pycache__"
    pyc_dir.mkdir(parents=True)
    (pyc_dir / "run.cpython-311.pyc").write_bytes(b"\x00\x01\x02")
    paths = {f["path"] for f in skill_files(tmp_path, "demo")}
    assert not any("__pycache__" in p for p in paths)
    assert "scripts/run.py" in paths  # real script still listed
