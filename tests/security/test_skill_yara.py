"""Tests for the optional YARA signature scan (durin/security/skill_yara.py).

Skips entirely when the [skill-yara] extra (yara-python) is not installed, so CI
without the extra stays green (matches the vector/embedding test posture).
"""
import pytest

pytest.importorskip("yara")
from durin.security import skill_yara

_RULE = 'rule m { strings: $a = "EVIL_PAYLOAD_MARKER" condition: $a }\n'


def test_scan_yara_matches(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "t.yar").write_text(_RULE)
    monkeypatch.setattr(skill_yara, "_rules_dir", lambda: rules_dir)
    skill = tmp_path / "skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: x\n---\nb")
    (skill / "scripts" / "p.bin").write_text("....EVIL_PAYLOAD_MARKER....")
    findings = skill_yara.scan_yara(skill)
    assert any(f.category == "yara_signature" for f in findings)


def test_scan_yara_no_match(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "t.yar").write_text(_RULE)
    monkeypatch.setattr(skill_yara, "_rules_dir", lambda: rules_dir)
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: x\n---\nclean")
    assert skill_yara.scan_yara(skill) == []
