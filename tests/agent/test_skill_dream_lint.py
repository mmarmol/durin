"""The dream never ships a bundled file that does not even parse.

Only a person's web save linted scripts; the dream's own write paths wrote
them as-is, so a script that failed `bash -n` or `compile()` went live.
"""
from __future__ import annotations

from pathlib import Path

from durin.agent import skills_store as ss


def _body(name: str) -> str:
    return f"---\nname: {name}\ndescription: does {name}\n---\n# {name}\n\nRun the script.\n"


def _auto_skill(ws: Path, name: str, files: dict[str, str] | None = None) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: the {name} procedure\n"
        f"metadata:\n  durin:\n    mode: auto\n---\n# {name}\n\nDo the steps.\n",
        encoding="utf-8")
    for rel, text in (files or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")


def test_a_new_skill_with_a_broken_shell_script_is_refused(tmp_path: Path) -> None:
    res = ss.dream_create_skill(tmp_path, "runner", _body("runner"), "from a gap",
                                files={"scripts/run.sh": "if then fi\n"})

    assert "error" in res
    assert "scripts/run.sh" in res["error"]
    assert not (tmp_path / "skills" / "runner").exists()


def test_an_edit_that_breaks_a_script_is_refused(tmp_path: Path) -> None:
    _auto_skill(tmp_path, "runner", {"scripts/run.py": "print('ok')\n"})

    res = ss.plan_skill_edit(tmp_path, "runner", old="print('ok')", new="print('ok'",
                             rationale="fix", file="scripts/run.py")

    assert "error" in res
    assert "scripts/run.py" in res["error"]


def test_a_fuse_with_a_broken_file_is_refused(tmp_path: Path) -> None:
    _auto_skill(tmp_path, "a")
    _auto_skill(tmp_path, "b")

    res = ss.dream_fuse_skills(tmp_path, target="c", content=_body("c"), sources=["a", "b"],
                               rationale="merge", files={"scripts/x.py": "def (\n"})

    assert "error" in res
    assert (tmp_path / "skills" / "a").exists()


def test_a_restructure_with_a_broken_file_is_refused(tmp_path: Path) -> None:
    _auto_skill(tmp_path, "a")

    res = ss.dream_restructure_skill(tmp_path, "a", content=_body("a"), rationale="lift code",
                                     files={"scripts/x.json": "{not json"})

    assert "error" in res
    assert not (tmp_path / "skills" / "a" / "scripts" / "x.json").exists()
