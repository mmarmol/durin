"""Task 2.3: skill search hits render under a dedicated SKILL section.

A skill hit (``type == "skill"``) carries a procedure (a SKILL.md)
matching the query. It gets its own ``=== SKILL: <name> ===`` block,
ordered first so the procedural "playbook" leads the output, with an
intro that frames it as instructions to follow rather than facts to
cite.

The three coupled section dicts (``_SECTION_FOR_TYPE``,
``_SECTION_ORDER``, ``_SECTION_INTRO``) must stay in lockstep — a
missing key in any of them KeyErrors and crashes all search rendering,
so the mixed-render test guards against that regression.
"""

from __future__ import annotations

from durin.memory.sectioned_output import SectionedHit, render_sectioned


def test_skill_hit_renders_under_skill_section() -> None:
    hit = SectionedHit(
        uri="skills/git/SKILL.md",
        type="skill",
        path="skills/git/SKILL.md",
        score=1.0,
        snippet="run git rebase -i",
        summary="rebase flow",
    )
    out = render_sectioned([hit])
    assert "=== SKILL: git ===" in out or "SKILL:" in out
    assert "rebase" in out


def test_render_mixed_does_not_raise() -> None:
    skill = SectionedHit(
        uri="skills/x/SKILL.md",
        type="skill",
        path="skills/x/SKILL.md",
        score=1.0,
        snippet="s",
    )
    frag = SectionedHit(
        uri="memory/episodic/1",
        type="episodic",
        path="memory/episodic/1.md",
        score=0.9,
        snippet="f",
    )
    out = render_sectioned([skill, frag])
    assert "SKILL" in out
