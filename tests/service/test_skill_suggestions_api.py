"""Task 4: SkillsService skill-suggestion endpoints.

Tests the list/accept/reject surface introduced in:
  GET  /api/v1/skills/suggestions
  POST /api/v1/skills/suggestions/{id}/accept
  POST /api/v1/skills/suggestions/{id}/reject

Uses the same direct-construction pattern as test_skills.py:
  SkillsService(workspace=...) + Principal.local()
"""

from __future__ import annotations

import pytest

from durin.agent import skill_suggestions as sg
from durin.service.principal import Principal
from durin.service.skills import (
    AcceptSuggestionCommand,
    RejectSuggestionCommand,
    SkillSuggestionsQuery,
    SkillsService,
)


@pytest.mark.asyncio
async def test_list_accept_reject_roundtrip(tmp_path):
    ws = tmp_path
    (ws / "skills" / "x").mkdir(parents=True)
    (ws / "skills" / "x" / "SKILL.md").write_text(
        "---\nname: x\ndescription: d\ndurin:\n  mode: manual\n---\nold body\n",
        encoding="utf-8",
    )
    action = {
        "type": "evolve",
        "name": "x",
        "old": "old body",
        "new": "new body",
        "rationale": "improve",
    }
    rec = sg.add_suggestion(ws, action)

    svc = SkillsService(workspace=ws)
    pr = Principal.local()

    listed = await svc.suggestions(SkillSuggestionsQuery(), pr)
    assert len(listed.suggestions) == 1
    assert listed.suggestions[0].id == rec["id"]

    await svc.accept_suggestion(AcceptSuggestionCommand(id=rec["id"]), pr)
    assert "new body" in (ws / "skills" / "x" / "SKILL.md").read_text()
    assert sg.read_suggestions(ws) == []

    rec2 = sg.add_suggestion(ws, action)
    await svc.reject_suggestion(RejectSuggestionCommand(id=rec2["id"]), pr)
    assert sg.read_suggestions(ws) == []
    assert sg.is_tombstoned(ws, rec2["id"]) is True
