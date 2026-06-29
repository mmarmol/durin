# tests/agent/test_skill_suggestions.py
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from durin.agent import skill_suggestions as sg
from durin.agent import skills_store as ss


def _ws(tmp_path: Path) -> Path:
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _manual_skill(ws: Path, name: str, body: str = "Do the thing.\n") -> Path:
    skill_dir = ws / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\ndurin:\n  mode: manual\n---\n{body}"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


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


def test_tombstone_blocks_then_expires(tmp_path):
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


def test_apply_suggestion_evolve(tmp_path):
    ws = _ws(tmp_path)
    name = "my-skill"
    _manual_skill(ws, name, body="old text\n")
    action = {"type": "evolve", "name": name, "old": "old text", "new": "new text", "rationale": "r"}
    res = sg.apply_suggestion(ws, action)
    assert "error" not in res
    content = ss.read_skill_content(ws, name)
    assert content is not None
    assert "new text" in content


def test_apply_suggestion_retire(tmp_path):
    ws = _ws(tmp_path)
    name = "gone-skill"
    _manual_skill(ws, name)
    action = {"type": "retire", "name": name}
    res = sg.apply_suggestion(ws, action)
    assert "error" not in res
    assert ss.read_skill_content(ws, name) is None


def test_apply_suggestion_unknown_type_returns_error(tmp_path):
    ws = _ws(tmp_path)
    res = sg.apply_suggestion(ws, {"type": "bogus"})
    assert "error" in res


def test_tombstone_real_time_expiry(tmp_path):
    ws = _ws(tmp_path)
    fp = "fp1"
    sg.add_tombstone(ws, fp)
    # Overwrite tombstone file with a timestamp ~40 days in the past
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    tombstone_path = ws / "skills" / ".suggestion_tombstones.json"
    tombstone_path.write_text(json.dumps({fp: old_ts}), encoding="utf-8")
    # Should no longer be tombstoned (TTL=30 days, entry is 40 days old)
    assert sg.is_tombstoned(ws, fp) is False
    # Expired entry must have been purged from the file
    remaining = json.loads(tombstone_path.read_text(encoding="utf-8"))
    assert fp not in remaining
