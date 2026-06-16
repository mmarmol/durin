from pathlib import Path

from durin.agent.skills_store import Attribution, save_skill_file, set_mode, skill_history


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
