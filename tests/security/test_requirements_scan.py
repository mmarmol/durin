# tests/security/test_requirements_scan.py
from pathlib import Path

from durin.security.requirements_scan import extract_requirements


def _write_skill(tmp_path: Path, name: str, frontmatter: str, body: str = "Hello.") -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}\n")
    return d


def test_step1_declared_platforms_and_requires(tmp_path):
    d = _write_skill(tmp_path, "gh-skill", """
name: gh-skill
platforms: [macos, linux]
metadata:
  durin:
    requires:
      bins: [gh, jq]
      env: [GITHUB_TOKEN]
""")
    req = extract_requirements(d)
    assert req["platforms"]["value"] == ["macos", "linux"]
    assert req["platforms"]["inferred"] is False
    bin_names = [b["name"] for b in req["bins"]]
    assert "gh" in bin_names and "jq" in bin_names
    assert all(b["origin"] == "declared" for b in req["bins"])
    assert req["env"][0]["name"] == "GITHUB_TOKEN"
    assert req["env"][0]["origin"] == "declared"
    assert req["compatibility"] == ""


def test_step1_no_frontmatter_returns_empty_manifest(tmp_path):
    d = _write_skill(tmp_path, "bare", "name: bare", "Just text.")
    req = extract_requirements(d)
    assert req["platforms"]["value"] == []
    assert req["bins"] == []
    assert req["env"] == []


def test_step1_compatibility_field_preserved(tmp_path):
    d = _write_skill(tmp_path, "c", """
name: c
compatibility: "Requires macOS 12+ and homebrew."
""")
    req = extract_requirements(d)
    assert req["compatibility"] == "Requires macOS 12+ and homebrew."


def test_empty_skill_dir_does_not_crash(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    req = extract_requirements(d)
    assert req["bins"] == []
