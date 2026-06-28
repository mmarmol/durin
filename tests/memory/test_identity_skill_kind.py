from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_IDENTITY = _ROOT / "durin" / "templates" / "agent" / "identity.md"
_DOC06 = _ROOT / "docs" / "internals" / "memory" / "06_prompts_and_instructions.md"
_SKILLS_SECTION = _ROOT / "durin" / "templates" / "agent" / "skills_section.md"

def _norm(p: Path) -> str:
    return re.sub(r"\s+", " ", p.read_text(encoding="utf-8"))

def test_identity_lists_skill_as_fifth_kind():
    t = _norm(_IDENTITY)
    assert "Skills" in t
    # the skill kind is described as procedural / followed-not-cited
    assert "procedur" in t.lower()

def test_identity_search_results_say_follow_not_cite():
    t = _norm(_IDENTITY).lower()
    # the skill kind is described as followed-not-cited in the ## Memory block
    assert "follow" in t and "skill" in t

def test_doc06_skill_bullet_in_sync_with_identity():
    # the skill kind bullet must appear in BOTH identity.md and the prompts doc
    assert "Skills" in _norm(_IDENTITY)
    assert "Skills" in _norm(_DOC06)

def test_skills_section_names_both_surfaces():
    t = _norm(_SKILLS_SECTION).lower()
    # always-on catalog (read_file) AND searchable via memory_search kind=skill
    assert "memory_search" in t
    assert "read" in t  # read_file catalog surface

def test_skills_section_reframed_as_working_set_with_search_nudge():
    t = _norm(_SKILLS_SECTION).lower()
    # the stale "always available [full catalog]" claim is gone
    assert "always available" not in t
    # nudge: search when the shown skills don't cover the task
    assert "memory_search" in t
    assert ("if nothing" in t or "if none" in t or "don't cover" in t
            or "doesn't cover" in t)
    assert ("before" in t and ("proceed" in t or "conclud" in t or "say" in t))
