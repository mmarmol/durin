"""Bundled files on skill authoring: written path-safely, scanned before the
skill activates; risky code quarantines instead of installing."""
import json

from durin.agent.skills_store import dream_create_skill

BODY = """---
name: convert-notes
description: Convert meeting notes into the team format. Use when asked to reformat notes.
---
# Convert Notes

Run `scripts/convert.py` on the notes file.
"""


def test_safe_script_installs_with_verdict_stamped(tmp_path):
    out = dream_create_skill(tmp_path, "convert-notes", BODY, "recurring conversion",
                             files={"scripts/convert.py": "print('converted')\n"})
    assert out.get("ok") is True
    sdir = tmp_path / "skills" / "convert-notes"
    assert (sdir / "scripts" / "convert.py").read_text(encoding="utf-8") == "print('converted')\n"
    md = (sdir / "SKILL.md").read_text(encoding="utf-8")
    assert "scan_verdict: safe" in md


def test_risky_script_is_quarantined_not_activated(tmp_path):
    risky = "import os\ntoken = os.environ['SECRET']\n"      # environment access → caution
    out = dream_create_skill(tmp_path, "convert-notes", BODY, "r",
                             files={"scripts/convert.py": risky})
    assert out.get("quarantined") is True
    assert out["verdict"] in ("caution", "dangerous")
    assert out["findings"]                                    # the reasons travel with the result
    assert not (tmp_path / "skills" / "convert-notes").exists()
    qdir = tmp_path / ".durin" / "import-quarantine" / "convert-notes"
    assert (qdir / "SKILL.md").is_file()
    scan = json.loads((qdir / ".scan.json").read_text(encoding="utf-8"))
    assert scan["source"] == "authored:agent"
    assert scan["verdict"] == out["verdict"]


def test_traversal_paths_rejected_before_anything_lands(tmp_path):
    for bad in ("../escape.py", "/abs.py", "a/../../b.py"):
        out = dream_create_skill(tmp_path, "convert-notes", BODY, "r",
                                 files={bad: "print('x')\n"})
        assert "invalid bundled file path" in out["error"]
    assert not (tmp_path / "skills" / "convert-notes").exists()
    assert not (tmp_path / "escape.py").exists()


def test_no_files_path_unchanged_no_scan_stamp(tmp_path):
    out = dream_create_skill(tmp_path, "convert-notes", BODY, "r")
    assert out.get("ok") is True
    md = (tmp_path / "skills" / "convert-notes" / "SKILL.md").read_text(encoding="utf-8")
    assert "scan_verdict" not in md
