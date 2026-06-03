# tests/agent/test_skill_interop_roundtrip.py
"""Round-trip fidelity: FOREIGN frontmatter survives every durin skill mutation.

agentskills.io interop keystone — an imported standard skill carries root keys
(version/license/platforms/allowed-tools/...) and foreign vendor blocks
(metadata.hermes.*, metadata.somevendor.*) that durin does not own. Editing a
skill (mode flip, body edit, curate, save, namespace stamp) must never silently
drop that data. All mutations route through `_update_md`
(split_frontmatter -> mutate -> join_frontmatter) or overwrite verbatim, so the
full parsed dict round-trips.
"""
from durin.agent import skills_store as ss
from durin.agent.skills_frontmatter import split_frontmatter

FOREIGN_SKILL = """---
name: imported-thing
description: An imported standard skill.
version: 2.1.0
license: MIT
platforms: [linux, macos]
allowed-tools: Bash Read
compatibility: needs python>=3.10
metadata:
  hermes:
    tags: [qa, browser]
    requires_toolsets: [web]
  somevendor:
    custom: keep-me
x-unknown-root: preserve-this
---

# Imported Thing

Step 1. do the thing.
"""

_FOREIGN_KEYS = ("version", "license", "platforms", "allowed-tools",
                 "compatibility", "x-unknown-root")


def _assert_foreign_intact(text):
    data, _ = split_frontmatter(text)
    for k in _FOREIGN_KEYS:
        assert k in data, f"lost root key {k!r}"
    assert data["version"] == "2.1.0"
    assert data["metadata"]["hermes"]["tags"] == ["qa", "browser"]
    assert data["metadata"]["hermes"]["requires_toolsets"] == ["web"]
    assert data["metadata"]["somevendor"]["custom"] == "keep-me"
    assert data["x-unknown-root"] == "preserve-this"


def _seed(workspace, name="imported-thing", content=FOREIGN_SKILL):
    d = workspace / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")


def _read(workspace, name="imported-thing"):
    return (workspace / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def test_set_mode_preserves_foreign(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(ws)
    ss.set_mode(ws, "imported-thing", "manual")
    _assert_foreign_intact(_read(ws))


def test_apply_edit_preserves_foreign(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(ws)
    ss.set_mode(ws, "imported-thing", "manual")
    res = ss.apply_skill_edit(
        ws, "imported-thing",
        old="do the thing", new="do the improved thing",
        rationale="clarify step", confirm=True,
    )
    assert res["ok"] is True
    text = _read(ws)
    _assert_foreign_intact(text)
    assert "do the improved thing" in text


def test_mark_curated_preserves_foreign(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(ws)
    ss.mark_curated(ws, "imported-thing")
    _assert_foreign_intact(_read(ws))


def test_save_content_preserves_what_user_writes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(ws)
    ss.set_mode(ws, "imported-thing", "manual")
    res = ss.save_skill_content(ws, "imported-thing", FOREIGN_SKILL, rationale="r")
    assert res["ok"] is True
    _assert_foreign_intact(_read(ws))


def test_durin_namespace_added_without_clobbering(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(ws)
    ss.set_mode(ws, "imported-thing", "auto")
    data, _ = split_frontmatter(_read(ws))
    assert data["metadata"]["durin"]["mode"] == "auto"
    assert data["metadata"]["hermes"]["tags"] == ["qa", "browser"]
