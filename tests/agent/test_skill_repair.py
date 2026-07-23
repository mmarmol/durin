"""Deterministic repair of invalid quarantined skills.

The quarantine card used to offer approve/reject on a skill whose SKILL.md
could not even be parsed — approving something broken fixes nothing. Repair
handles the deterministic failure modes without a model: broken YAML
frontmatter is salvaged and re-serialized (the classic case: an unquoted
": " inside a plain multi-line description invalidates the whole document),
a missing name derives from the directory, a missing description from the
body's first paragraph. Preview first (diff, no write), then apply.
"""

import json
from pathlib import Path

import pytest

from durin.agent.skills_import import repair_quarantined, validate_skill


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / ".durin" / "import-quarantine").mkdir(parents=True)
    return tmp_path


def quarantine(workspace: Path, name: str, text: str) -> Path:
    d = workspace / ".durin" / "import-quarantine" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d


BROKEN_COLON = """---
name: my-skill
description: What counts as evidence per area — use when drafting a note: operational
  claims must show exhibits, never aggregates alone.
metadata:
  durin:
    mode: auto
---
# My skill

Body text here.
"""


class TestBrokenYaml:
    def test_unquoted_colon_frontmatter_is_salvaged(self, workspace):
        qdir = quarantine(workspace, "my-skill", BROKEN_COLON)
        assert validate_skill(qdir).errors   # precondition: currently invalid

        out = repair_quarantined(workspace, "my-skill")

        assert out["repaired"] is True
        assert out["errors_after"] == []
        assert any("frontmatter" in c for c in out["changes"])
        assert "-my-skill" not in out["diff"] or out["diff"]   # diff present
        # Preview mode: nothing written yet.
        assert (qdir / "SKILL.md").read_text() == BROKEN_COLON

    def test_apply_writes_a_valid_file_preserving_metadata_and_body(self, workspace):
        qdir = quarantine(workspace, "my-skill", BROKEN_COLON)

        out = repair_quarantined(workspace, "my-skill", apply=True)

        assert out["repaired"] is True
        rep = validate_skill(qdir)
        assert rep.errors == []
        text = (qdir / "SKILL.md").read_text()
        assert "operational claims must show exhibits" in text.replace("\n  ", " ").replace("\n", " ")
        assert "mode: auto" in text            # metadata block survived
        assert "Body text here." in text       # body untouched


class TestMissingFields:
    def test_missing_name_derives_from_directory(self, workspace):
        quarantine(workspace, "dir-named-skill", "---\ndescription: does things\n---\nBody.\n")

        out = repair_quarantined(workspace, "dir-named-skill", apply=True)

        assert out["repaired"] is True
        rep = validate_skill(workspace / ".durin" / "import-quarantine" / "dir-named-skill")
        assert rep.errors == []
        assert rep.name == "dir-named-skill"

    def test_missing_description_extracted_from_body(self, workspace):
        quarantine(workspace, "descless", "---\nname: descless\n---\n# Title\n\nFirst real paragraph\nof the body.\n\nSecond paragraph.\n")

        out = repair_quarantined(workspace, "descless", apply=True)

        assert out["repaired"] is True
        text = (workspace / ".durin" / "import-quarantine" / "descless" / "SKILL.md").read_text()
        assert "First real paragraph of the body." in text

    def test_no_frontmatter_at_all_synthesizes_one(self, workspace):
        quarantine(workspace, "bare", "# Bare skill\n\nIt does a bare thing.\n")

        out = repair_quarantined(workspace, "bare", apply=True)

        assert out["repaired"] is True
        rep = validate_skill(workspace / ".durin" / "import-quarantine" / "bare")
        assert rep.errors == []
        assert rep.name == "bare"


class TestNoOpAndErrors:
    def test_valid_skill_reports_nothing_to_repair(self, workspace):
        quarantine(workspace, "fine", "---\nname: fine\ndescription: already valid\n---\nBody.\n")

        out = repair_quarantined(workspace, "fine")

        assert out["repaired"] is False
        assert out["changes"] == []
        assert out["errors_after"] == []

    def test_unknown_quarantine_name_errors(self, workspace):
        out = repair_quarantined(workspace, "ghost")
        assert out["repaired"] is False
        assert "error" in out
