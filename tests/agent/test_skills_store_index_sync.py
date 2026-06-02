"""skills_store keeps the memory index in sync on every mutation.

Building a real lance index per unit test is heavy, so these tests
monkeypatch ``_sync_index`` / ``_unsync_index`` to record their calls and
assert each mutation fans out to the index with the right skill name.
The real implementations are guarded no-ops without an index (see
``test_skills_store_dream.py`` et al. for the on-disk-only behaviour).
"""
from __future__ import annotations

from durin.agent import skills_store as ss


def _spy(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(ss, "_sync_index", lambda ws, name: calls.append(("sync", name)))
    monkeypatch.setattr(ss, "_unsync_index", lambda ws, name: calls.append(("unsync", name)))
    return calls


def _write_skill(ws, name, mode, body="body"):
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\nmetadata:\n  durin:\n    mode: {mode}\n---\n{body}\n",
        encoding="utf-8",
    )
    return d


def test_create_syncs(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)
    ss.dream_create_skill(tmp_path / "ws", "git-helper", "# Git\n\nbody\n", rationale="r")
    assert ("sync", "git-helper") in calls


def test_apply_edit_ok_syncs_proposed_does_not(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _write_skill(ws, "auto-skill", "auto", body="old body")
    calls = _spy(monkeypatch)
    res = ss.apply_skill_edit(ws, "auto-skill", old="old body", new="new body", rationale="r")
    assert res.get("ok") is True
    assert ("sync", "auto-skill") in calls

    # manual + no confirm => proposed, NO write, NO sync
    _write_skill(ws, "manual-skill", "manual", body="x")
    calls2 = _spy(monkeypatch)
    res2 = ss.apply_skill_edit(ws, "manual-skill", old="x", new="y", rationale="r")
    assert res2.get("proposed") is True
    assert calls2 == []


def test_apply_edit_manual_confirm_syncs(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _write_skill(ws, "manual-skill", "manual", body="x")
    calls = _spy(monkeypatch)
    res = ss.apply_skill_edit(
        ws, "manual-skill", old="x", new="y", rationale="r", confirm=True
    )
    assert res.get("ok") is True
    assert ("sync", "manual-skill") in calls


def test_save_content_syncs(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _write_skill(ws, "manual-skill", "manual", body="x")
    calls = _spy(monkeypatch)
    res = ss.save_skill_content(
        ws, "manual-skill",
        "---\nname: manual-skill\nmetadata:\n  durin:\n    mode: manual\n---\nnew\n",
    )
    assert res.get("ok") is True
    assert ("sync", "manual-skill") in calls


def test_set_mode_syncs(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _write_skill(ws, "auto-skill", "auto")
    calls = _spy(monkeypatch)
    ss.set_mode(ws, "auto-skill", "manual")
    assert ("sync", "auto-skill") in calls


def test_mark_curated_syncs(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _write_skill(ws, "auto-skill", "auto")
    calls = _spy(monkeypatch)
    ss.mark_curated(ws, "auto-skill")
    assert ("sync", "auto-skill") in calls


def test_fuse_syncs_target_unsyncs_sources(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    for n in ("a", "b"):
        _write_skill(ws, n, "auto", body=f"body {n}")
    calls = _spy(monkeypatch)
    res = ss.dream_fuse_skills(
        ws, target="c", content="# C\n\nm\n", sources=["a", "b"], rationale="r"
    )
    assert res.get("ok") is True
    assert ("sync", "c") in calls
    assert ("unsync", "a") in calls
    assert ("unsync", "b") in calls
