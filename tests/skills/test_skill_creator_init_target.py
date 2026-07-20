import subprocess
import sys
from pathlib import Path

SCRIPT = Path("durin/skills/skill-creator/scripts/init_skill.py")


def test_init_rejects_skills_registry(tmp_path):
    reg = tmp_path / "skills"
    reg.mkdir()
    r = subprocess.run([sys.executable, str(SCRIPT), "demo", "--path", str(reg)],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "skill-drafts" in (r.stdout + r.stderr)


def test_init_allows_drafts(tmp_path):
    drafts = tmp_path / "skill-drafts"
    drafts.mkdir()
    r = subprocess.run([sys.executable, str(SCRIPT), "demo", "--path", str(drafts)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert (drafts / "demo" / "SKILL.md").exists()
