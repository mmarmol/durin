import importlib
import shutil
import sys
import zipfile
from pathlib import Path


SCRIPT_DIR = Path("durin/skills/skill-creator/scripts").resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

init_skill = importlib.import_module("init_skill")
package_skill = importlib.import_module("package_skill")
quick_validate = importlib.import_module("quick_validate")


def test_init_skill_creates_expected_files(tmp_path: Path) -> None:
    skill_dir = init_skill.init_skill(
        "demo-skill",
        tmp_path,
        ["scripts", "references", "assets"],
        include_examples=True,
    )

    assert skill_dir == tmp_path / "demo-skill"
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "scripts" / "example.py").exists()
    assert (skill_dir / "references" / "api_reference.md").exists()
    assert (skill_dir / "assets" / "example_asset.txt").exists()


def test_validate_skill_accepts_existing_skill_creator() -> None:
    valid, message = quick_validate.validate_skill(
        Path("durin/skills/skill-creator").resolve()
    )

    assert valid, message


def test_validate_skill_rejects_placeholder_description(tmp_path: Path) -> None:
    skill_dir = tmp_path / "placeholder-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: placeholder-skill\n"
        'description: "[TODO: fill me in]"\n'
        "---\n"
        "# Placeholder\n",
        encoding="utf-8",
    )

    valid, message = quick_validate.validate_skill(skill_dir)

    assert not valid
    assert "TODO placeholder" in message


def test_validate_skill_rejects_root_files_outside_allowed_dirs(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bad-root-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: bad-root-skill\n"
        "description: Valid description\n"
        "---\n"
        "# Skill\n",
        encoding="utf-8",
    )
    (skill_dir / "README.md").write_text("extra\n", encoding="utf-8")

    valid, message = quick_validate.validate_skill(skill_dir)

    assert not valid
    assert "Unexpected file or directory in skill root" in message


def test_package_skill_creates_archive(tmp_path: Path) -> None:
    skill_dir = tmp_path / "package-me"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: package-me\n"
        "description: Package this skill.\n"
        "---\n"
        "# Skill\n",
        encoding="utf-8",
    )
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "helper.py").write_text("print('ok')\n", encoding="utf-8")

    archive_path = package_skill.package_skill(skill_dir, tmp_path / "dist")

    assert archive_path == (tmp_path / "dist" / "package-me.skill")
    assert archive_path.exists()
    with zipfile.ZipFile(archive_path, "r") as archive:
        names = set(archive.namelist())
    assert "package-me/SKILL.md" in names
    assert "package-me/scripts/helper.py" in names


def test_package_skill_rejects_symlink(tmp_path: Path) -> None:
    skill_dir = tmp_path / "symlink-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: symlink-skill\n"
        "description: Reject symlinks during packaging.\n"
        "---\n"
        "# Skill\n",
        encoding="utf-8",
    )
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("secret\n", encoding="utf-8")
    link = scripts_dir / "outside.txt"

    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return

    archive_path = package_skill.package_skill(skill_dir, tmp_path / "dist")

    assert archive_path is None
    assert not (tmp_path / "dist" / "symlink-skill.skill").exists()


def _write_skill(skill_dir: Path, name: str, body: str = "# Skill\n", description: str = "Valid description") -> None:
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )


def test_validate_skill_reports_all_errors_not_just_first(tmp_path: Path) -> None:
    skill_dir = tmp_path / "multi-error-skill"
    _write_skill(skill_dir, "multi-error-skill", description='"[TODO: fill me in]"')
    (skill_dir / "README.md").write_text("extra\n", encoding="utf-8")

    valid, message = quick_validate.validate_skill(skill_dir)

    assert not valid
    assert "TODO placeholder" in message
    assert "Unexpected file or directory in skill root" in message


def test_validate_skill_rejects_broken_resource_link(tmp_path: Path) -> None:
    skill_dir = tmp_path / "broken-link-skill"
    _write_skill(
        skill_dir,
        "broken-link-skill",
        body="# Skill\n\nSee [the rubric](references/missing.md) for details.\n",
    )

    valid, message = quick_validate.validate_skill(skill_dir)

    assert not valid
    assert "Linked resource does not exist: references/missing.md" in message


def test_validate_skill_accepts_resolved_link_and_ignores_inline_code(tmp_path: Path) -> None:
    skill_dir = tmp_path / "good-link-skill"
    _write_skill(
        skill_dir,
        "good-link-skill",
        body=(
            "# Skill\n\nSee [the rubric](references/rubric.md).\n"
            "Illustrative only: `references/imaginary.md` and `scripts/rotate_pdf.py`.\n"
        ),
    )
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "rubric.md").write_text("# Rubric\n", encoding="utf-8")

    valid, message = quick_validate.validate_skill(skill_dir)

    assert valid, message


def test_validate_skill_warns_on_orphan_reference(tmp_path: Path) -> None:
    skill_dir = tmp_path / "orphan-skill"
    _write_skill(skill_dir, "orphan-skill")
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "lonely.md").write_text("# Never cited\n", encoding="utf-8")

    valid, message = quick_validate.validate_skill(skill_dir)

    assert valid  # warning, not error
    assert "WARNING: Resource never mentioned in SKILL.md: references/lonely.md" in message


def test_validate_skill_mention_in_code_block_counts(tmp_path: Path) -> None:
    skill_dir = tmp_path / "mentioned-skill"
    _write_skill(
        skill_dir,
        "mentioned-skill",
        body="# Skill\n\nRun:\n```bash\npython scripts/helper.py --quiet\n```\n",
    )
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "helper.py").write_text("print('ok')\n", encoding="utf-8")

    valid, message = quick_validate.validate_skill(skill_dir)

    assert valid, message
    assert "WARNING" not in message


def test_validate_skill_rejects_python_syntax_error(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bad-script-skill"
    _write_skill(
        skill_dir,
        "bad-script-skill",
        body="# Skill\n\nUses scripts/broken.py for processing.\n",
    )
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "broken.py").write_text("def oops(:\n", encoding="utf-8")

    valid, message = quick_validate.validate_skill(skill_dir)

    assert not valid
    assert "Script has syntax errors: broken.py" in message


def test_validate_skill_never_executes_scripts(tmp_path: Path) -> None:
    skill_dir = tmp_path / "sentinel-skill"
    _write_skill(
        skill_dir,
        "sentinel-skill",
        body="# Skill\n\nUses scripts/sentinel.py for processing.\n",
    )
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    sentinel = tmp_path / "executed.flag"
    (scripts / "sentinel.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n",
        encoding="utf-8",
    )

    valid, message = quick_validate.validate_skill(skill_dir)

    assert valid, message
    assert not sentinel.exists(), "validator must never execute skill scripts"


def test_validate_skill_warns_on_oversized_skill_md(tmp_path: Path) -> None:
    skill_dir = tmp_path / "oversized-skill"
    long_body = "# Skill\n" + ("filler line\n" * 520)
    _write_skill(skill_dir, "oversized-skill", body=long_body)

    valid, message = quick_validate.validate_skill(skill_dir)

    assert valid  # warning, not error
    assert "WARNING: SKILL.md is" in message
    assert "500" in message


def test_init_skill_template_has_rubric_stubs_and_no_codex(tmp_path: Path) -> None:
    skill_dir = init_skill.init_skill("stub-skill", tmp_path, [], include_examples=False)

    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "## Common Pitfalls" in content
    assert "## Verification Checklist" in content
    assert "Codex" not in content
