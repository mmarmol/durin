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
