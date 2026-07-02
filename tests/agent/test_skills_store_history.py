from pathlib import Path

from durin.agent.skills_store import (
    Attribution,
    mark_curated,
    save_skill_file,
    set_mode,
    skill_history,
    user_edits_since_curation,
)


def _mk(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: x\n---\nbody\n", encoding="utf-8")


def test_skill_history_parses_trailers_and_provenance(tmp_path: Path):
    _mk(tmp_path, "demo")
    set_mode(tmp_path, "demo", "manual")  # first commit
    save_skill_file(tmp_path, "demo", "SKILL.md", "v2\n", rationale="edited SKILL.md via web",
                    attribution=Attribution(actor="user"))
    save_skill_file(tmp_path, "demo", "SKILL.md", "v3\n", rationale="tweak",
                    attribution=Attribution(actor="agent", session="s9", agent="claude-opus-4-8"))

    hist = skill_history(tmp_path, "demo")
    assert "provenance" in hist and isinstance(hist["commits"], list)
    top = hist["commits"][0]
    assert top["actor"] == "agent" and top["session"] == "s9" and top["agent"] == "claude-opus-4-8"
    assert top["subject"] == "skill(demo): tweak"


def test_skill_history_derives_actor_for_trailerless_commit(tmp_path: Path):
    _mk(tmp_path, "demo")
    # set_mode commits with no trailers -> derived actor "system"
    set_mode(tmp_path, "demo", "manual")
    hist = skill_history(tmp_path, "demo")
    sysrow = [c for c in hist["commits"] if c["subject"].endswith("set mode=manual")][0]
    assert sysrow["actor"] == "system" and sysrow["session"] is None


def test_skill_history_unknown_skill(tmp_path: Path):
    assert skill_history(tmp_path, "nope") == {"provenance": {}, "commits": []}


def test_user_edits_since_curation_stops_at_last_stamp(tmp_path: Path):
    _mk(tmp_path, "demo")
    set_mode(tmp_path, "demo", "auto")
    # A user edit BEFORE curation should not count once we've curated past it.
    save_skill_file(tmp_path, "demo", "SKILL.md", "old\n", rationale="edited SKILL.md via web",
                    attribution=Attribution(actor="user"))
    mark_curated(tmp_path, "demo")
    # A fresh user edit AFTER the curation stamp is what dream must respect.
    save_skill_file(tmp_path, "demo", "SKILL.md", "new\n", rationale="edited SKILL.md via web",
                    attribution=Attribution(actor="user"))

    edits = user_edits_since_curation(tmp_path, "demo")
    assert len(edits) == 1
    assert edits[0]["subject"] == "skill(demo): edited SKILL.md via web"


def test_user_edits_since_curation_ignores_non_user(tmp_path: Path):
    _mk(tmp_path, "demo")
    set_mode(tmp_path, "demo", "auto")
    save_skill_file(tmp_path, "demo", "SKILL.md", "v2\n", rationale="tweak",
                    attribution=Attribution(actor="agent", session="s1", agent="m"))
    assert user_edits_since_curation(tmp_path, "demo") == []
