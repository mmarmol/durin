# tests/agent/test_skill_suggestions.py
from pathlib import Path

from durin.agent import skill_suggestions as sg


def _ws(tmp_path: Path) -> Path:
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_fingerprint_ignores_reason_text(tmp_path):
    a = {"type": "evolve", "name": "x", "old": "a", "new": "b", "rationale": "because foo"}
    b = {"type": "evolve", "name": "x", "old": "a", "new": "b", "rationale": "totally different wording"}
    assert sg.fingerprint(a) == sg.fingerprint(b)


def test_fingerprint_changes_with_proposed_content(tmp_path):
    a = {"type": "evolve", "name": "x", "old": "a", "new": "b"}
    c = {"type": "evolve", "name": "x", "old": "a", "new": "c"}
    assert sg.fingerprint(a) != sg.fingerprint(c)


def test_make_patch_evolve_is_unified_diff(tmp_path):
    patch = sg.make_patch({"type": "evolve", "name": "x", "old": "hello\nworld", "new": "hello\nthere"})
    assert "--- a/SKILL.md" in patch
    assert "+++ b/SKILL.md" in patch
    assert "-world" in patch and "+there" in patch


def test_make_patch_retire_is_none(tmp_path):
    assert sg.make_patch({"type": "retire", "name": "x"}) is None


def test_add_read_remove_suggestion(tmp_path):
    ws = _ws(tmp_path)
    rec = sg.add_suggestion(ws, {"type": "evolve", "name": "x", "old": "a", "new": "b", "rationale": "r"})
    assert rec["id"] == sg.fingerprint({"type": "evolve", "name": "x", "old": "a", "new": "b"})
    assert rec["patch"] and rec["reason"] == "r" and rec["skill"] == "x"
    assert len(sg.read_suggestions(ws)) == 1
    sg.remove_suggestion(ws, rec["id"])
    assert sg.read_suggestions(ws) == []


def test_add_suggestion_dedups_by_fingerprint(tmp_path):
    ws = _ws(tmp_path)
    sg.add_suggestion(ws, {"type": "evolve", "name": "x", "old": "a", "new": "b"})
    sg.add_suggestion(ws, {"type": "evolve", "name": "x", "old": "a", "new": "b"})
    assert len(sg.read_suggestions(ws)) == 1


def test_tombstone_blocks_then_expires(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    fp = "abc123"
    sg.add_tombstone(ws, fp)
    assert sg.is_tombstoned(ws, fp) is True
    assert sg.is_tombstoned(ws, fp, ttl_days=0) is False  # already past a 0-day ttl


def test_needs_suggestion_tracks_body_changes(tmp_path):
    ws = _ws(tmp_path)
    skill_dir = ws / "skills" / "x"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: x\n---\nbody one\n", encoding="utf-8")
    assert sg.needs_suggestion(ws, "x") is True
    sg.mark_suggested(ws, "x")
    assert sg.needs_suggestion(ws, "x") is False
    (skill_dir / "SKILL.md").write_text("---\nname: x\n---\nbody two\n", encoding="utf-8")
    assert sg.needs_suggestion(ws, "x") is True
