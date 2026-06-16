
from durin.agent.skills_import import validate_skill


def _mk(tmp, name, fm="", body="x", scripts=None):
    d = tmp / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n{fm}---\n{body}\n")
    if scripts:
        s = d / "scripts"
        s.mkdir()
        for fn in scripts:
            (s / fn).write_text("#!/bin/sh\necho hi\n")
    return d


def test_valid_skill_no_code(tmp_path):
    r = validate_skill(_mk(tmp_path, "clean"))
    assert r.ok and not r.errors and r.carries_code is False


def test_missing_description_is_error(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: bad\n---\nx\n")
    r = validate_skill(d)
    assert not r.ok and any("description" in e for e in r.errors)


def test_missing_skill_md_is_error(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    r = validate_skill(d)
    assert not r.ok and any("SKILL.md" in e for e in r.errors)


def test_scripts_dir_flags_code(tmp_path):
    r = validate_skill(_mk(tmp_path, "tool", scripts=["setup.sh"]))
    assert r.carries_code is True and "scripts/setup.sh" in r.code_artifacts


def test_install_spec_flags_code(tmp_path):
    fm = "metadata:\n  openclaw:\n    install:\n      - {kind: brew, formula: gh}\n"
    r = validate_skill(_mk(tmp_path, "ghskill", fm=fm))
    assert r.carries_code is True and any("install" in a for a in r.code_artifacts)


def test_nonconformant_name_is_warning_not_error(tmp_path):
    d = tmp_path / "Bad_Name"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: d\n---\nx\n")
    r = validate_skill(d)
    assert r.ok and any("name" in w for w in r.warnings)
