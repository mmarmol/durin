from durin.agent.skills_store import (
    Attribution,
    discard_draft_skill,
    publish_draft_skill,
    read_mode,
    read_skill_content,
)


def _draft(ws, name, body, files=None):
    d = ws / "skill-drafts" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    for rel, content in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


BODY = "---\nname: emailer\ndescription: parse email. use when a .eml needs reading.\n---\nrun scripts/p.py\n"


def test_publish_promotes_draft_to_active(tmp_path):
    _draft(tmp_path, "emailer", BODY, {"scripts/p.py": "print('ok')\n"})
    out = publish_draft_skill(tmp_path, "emailer",
                              attribution=Attribution(actor="agent", session="s1", agent="m1"))
    assert out.get("ok"), out
    assert not (tmp_path / "skill-drafts" / "emailer").exists()      # draft consumed
    assert (tmp_path / "skills" / "emailer" / "SKILL.md").exists()   # now active
    assert read_mode(tmp_path, "emailer") == "auto"
    assert "scan_verdict: safe" in read_skill_content(tmp_path, "emailer")


def test_publish_missing_draft_errors(tmp_path):
    out = publish_draft_skill(tmp_path, "nope")
    assert "error" in out


def test_discard_removes_draft(tmp_path):
    _draft(tmp_path, "emailer", BODY)
    out = discard_draft_skill(tmp_path, "emailer")
    assert out.get("ok") is True
    assert not (tmp_path / "skill-drafts" / "emailer").exists()


def test_publish_composition_reject_leaves_draft_intact(tmp_path):
    _draft(tmp_path, "emailer", BODY)

    def _reject(prompt: str) -> str:
        return "narrates a workflow\nNARRATION — should be a workflow"

    out = publish_draft_skill(tmp_path, "emailer", composition_judge=_reject)
    assert out.get("composition_rejected") is True
    assert (tmp_path / "skill-drafts" / "emailer").exists()          # draft untouched
    assert not (tmp_path / "skills" / "emailer").exists()            # nothing activated


def test_publish_refuses_when_active_skill_exists(tmp_path):
    active = tmp_path / "skills" / "emailer"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text("ORIGINAL", encoding="utf-8")
    _draft(tmp_path, "emailer", BODY)

    out = publish_draft_skill(tmp_path, "emailer")
    assert "already exists" in out.get("error", "")
    assert (tmp_path / "skill-drafts" / "emailer").exists()          # draft untouched
    assert (active / "SKILL.md").read_text(encoding="utf-8") == "ORIGINAL"  # not clobbered
