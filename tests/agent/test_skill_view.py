"""Tests for skill_view: the dedicated skill-load payload + its usage signal."""

from __future__ import annotations

import json
from pathlib import Path

from durin.agent.skill_usage import extract_skill_calls
from durin.agent.skills import SkillsLoader


def _write_skill(
    base: Path,
    name: str,
    *,
    durin_meta: dict | None = None,
    body: str = "# Skill\n\nDo the thing.",
    files: dict[str, str] | None = None,
) -> None:
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    lines = ["---", f"name: {name}", "description: a test skill"]
    if durin_meta is not None:
        lines.append("metadata: " + json.dumps({"durin": durin_meta}, separators=(",", ":")))
    lines += ["---", "", body]
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    for rel, content in (files or {}).items():
        p = skill_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _loader(tmp_path: Path) -> SkillsLoader:
    workspace = tmp_path / "ws"
    (workspace / "skills").mkdir(parents=True)
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    return SkillsLoader(workspace, builtin_skills_dir=builtin)


# --- usage attribution -------------------------------------------------------

def test_skill_view_is_a_view_call():
    messages = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "skill_view", "arguments": {"name": "weather-lookup"}}},
    ]}]
    assert extract_skill_calls(messages) == [
        {"skill": "weather-lookup", "op": "view", "turn": 1}]


def test_skill_view_without_name_is_ignored():
    messages = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "skill_view", "arguments": {}}},
    ]}]
    assert extract_skill_calls(messages) == []


# --- view_skill payload ------------------------------------------------------

def test_view_skill_returns_stripped_body_and_ready(tmp_path: Path):
    loader = _loader(tmp_path)
    _write_skill(loader.workspace_skills, "alpha", body="# Alpha\n\nStep one.")
    out = loader.view_skill("alpha")
    assert out is not None
    assert out["name"] == "alpha"
    assert out["content"] == "# Alpha\n\nStep one."  # frontmatter stripped
    assert out["readiness"] == {"ready": True}
    assert out["skill_dir"].endswith("/skills/alpha")
    assert "linked_files" not in out  # single-file skill


def test_view_skill_missing_returns_none(tmp_path: Path):
    assert _loader(tmp_path).view_skill("nope") is None


def test_view_skill_reports_missing_bin_routed_to_installer(tmp_path: Path):
    loader = _loader(tmp_path)
    _write_skill(loader.workspace_skills, "needsbin",
                 durin_meta={"requires": {"bins": ["definitely_missing_bin_xyz"]}})
    r = loader.view_skill("needsbin")["readiness"]
    assert r["ready"] is False
    assert r["missing_bins"] == ["definitely_missing_bin_xyz"]
    assert "skill_install_deps" in r["install_hint"]


def test_view_skill_reports_missing_env_routed_to_secret(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SKILL_VIEW_TEST_ENV", raising=False)
    loader = _loader(tmp_path)
    _write_skill(loader.workspace_skills, "needsenv",
                 durin_meta={"requires": {"env": ["SKILL_VIEW_TEST_ENV"]}})
    r = loader.view_skill("needsenv")["readiness"]
    assert r["ready"] is False
    assert r["missing_env"] == ["SKILL_VIEW_TEST_ENV"]
    assert "request_secret" in r["secret_hint"]


def test_view_skill_maps_bundled_files(tmp_path: Path):
    loader = _loader(tmp_path)
    _write_skill(loader.workspace_skills, "bundled",
                 files={"scripts/run.py": "print('hi')\n",
                        "references/api.md": "# API\n"})
    out = loader.view_skill("bundled")
    assert out["linked_files"]["scripts"] == ["scripts/run.py"]
    assert out["linked_files"]["references"] == ["references/api.md"]
    assert "usage_hint" in out


def test_view_skill_reads_a_bundled_file(tmp_path: Path):
    loader = _loader(tmp_path)
    _write_skill(loader.workspace_skills, "bundled",
                 files={"scripts/run.py": "print('hi')\n"})
    out = loader.view_skill("bundled", file_path="scripts/run.py")
    assert out == {"name": "bundled", "file": "scripts/run.py", "content": "print('hi')\n"}


def test_view_skill_rejects_path_traversal(tmp_path: Path):
    loader = _loader(tmp_path)
    _write_skill(loader.workspace_skills, "bundled", files={"scripts/run.py": "x\n"})
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    out = loader.view_skill("bundled", file_path="../../secret.txt")
    assert "error" in out
    assert "content" not in out


def test_view_skill_excludes_pycache_from_map(tmp_path: Path):
    loader = _loader(tmp_path)
    _write_skill(loader.workspace_skills, "bundled",
                 files={"scripts/run.py": "x\n",
                        "scripts/__pycache__/run.cpython-311.pyc": "\x00binary"})
    out = loader.view_skill("bundled")
    assert out["linked_files"]["scripts"] == ["scripts/run.py"]


def test_view_skill_binary_file_errors_not_crash(tmp_path: Path):
    loader = _loader(tmp_path)
    _write_skill(loader.workspace_skills, "bundled", files={"scripts/run.py": "x\n"})
    (loader.workspace_skills / "bundled" / "assets").mkdir()
    (loader.workspace_skills / "bundled" / "assets" / "img.bin").write_bytes(b"\xa7\x00\xff")
    out = loader.view_skill("bundled", file_path="assets/img.bin")
    assert "error" in out
    assert "content" not in out
