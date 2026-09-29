"""A skill file that starts with a shebang is executable on every path that
writes one: a person's save, the dream's create, a draft publish, an import.

Skill docs run bundled scripts directly (``scripts/probe.sh <domain>``); a
script written with the default mode fails there with "Permission denied".
"""

import stat
from pathlib import Path

from durin.agent import skills_store as ss
from durin.agent.skills_import import install_imported_skill

SHEBANG_SH = "#!/bin/bash\necho hi\n"
SHEBANG_PY = "#!/usr/bin/env python3\nprint('ok')\n"


def _executable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IXUSR)


def test_a_saved_script_with_a_shebang_is_executable(tmp_path):
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: probe a domain\n---\nRun scripts/probe.sh.\n",
        encoding="utf-8",
    )
    ss.set_mode(tmp_path, "demo", "auto")
    user = ss.Attribution(actor="user")

    assert ss.save_skill_file(tmp_path, "demo", "scripts/probe.sh", SHEBANG_SH,
                              rationale="add probe", attribution=user)["ok"]
    assert ss.save_skill_file(tmp_path, "demo", "references/notes.md", "notes\n",
                              rationale="add notes", attribution=user)["ok"]

    assert _executable(skill / "scripts" / "probe.sh")
    assert not _executable(skill / "references" / "notes.md")


def test_a_dream_created_script_with_a_shebang_is_executable(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    res = ss.dream_create_skill(
        ws, "prober", "# Probe\n\nRun scripts/probe.sh <domain>.\n", rationale="recurring probe",
        files={"scripts/probe.sh": SHEBANG_SH, "scripts/fields.json": "{}\n"},
    )
    assert res.get("ok"), res

    scripts = ws / "skills" / "prober" / "scripts"
    assert _executable(scripts / "probe.sh")
    assert not _executable(scripts / "fields.json")


def test_a_published_draft_script_with_a_shebang_is_executable(tmp_path):
    draft = tmp_path / "skill-drafts" / "emailer"
    (draft / "scripts").mkdir(parents=True)
    (draft / "SKILL.md").write_text(
        "---\nname: emailer\ndescription: parse email. use when a .eml needs reading.\n---\n"
        "run scripts/p.py\n",
        encoding="utf-8",
    )
    (draft / "scripts" / "p.py").write_text(SHEBANG_PY, encoding="utf-8")

    res = ss.publish_draft_skill(tmp_path, "emailer", attribution=ss.Attribution(actor="agent"))
    assert res.get("ok"), res

    assert _executable(tmp_path / "skills" / "emailer" / "scripts" / "p.py")


def test_an_imported_script_with_a_shebang_is_executable(tmp_path):
    quarantine = tmp_path / ".durin" / "import-quarantine" / "tool"
    (quarantine / "scripts").mkdir(parents=True)
    (quarantine / "SKILL.md").write_text(
        "---\nname: tool\ndescription: run a tool\n---\nRun scripts/run.sh.\n", encoding="utf-8",
    )
    (quarantine / "scripts" / "run.sh").write_text(SHEBANG_SH, encoding="utf-8")

    res = install_imported_skill(tmp_path, quarantine, source="github:x/y",
                                 allowlist=["github:x/"], confirmed=True)
    assert res["ok"]

    assert _executable(tmp_path / "skills" / "tool" / "scripts" / "run.sh")
