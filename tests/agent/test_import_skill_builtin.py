"""The import-skill orchestrator builtin (§6.B). It drives the skill_import tool
through resolve -> fetch -> gate -> install. As a shipped builtin it must itself
pass the §8.C scan (a skill that talks ABOUT prompt-injection must not trip the
injection rules)."""
from durin.agent.skills import BUILTIN_SKILLS_DIR
from durin.agent.skills_import import validate_skill
from durin.security.skill_scan import scan_skill

_DIR = BUILTIN_SKILLS_DIR / "import-skill"


def test_import_skill_validates():
    vr = validate_skill(_DIR)
    assert vr.ok
    assert vr.name == "import-skill"
    assert not vr.carries_code


def test_import_skill_scans_safe():
    assert scan_skill(_DIR).verdict == "safe"


def test_import_skill_drives_the_tool():
    body = (_DIR / "SKILL.md").read_text()
    assert "skill_import" in body
    for action in ("resolve", "fetch", "install", "reject"):
        assert action in body
    # the gate is surfaced, not worked around
    assert "override" in body and "confirm" in body
