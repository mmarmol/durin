"""A composition-gate bounce on the autonomous door is never silently lost:
it lands in the suggestions bandeja annotated; a compliant landing of the same
name clears the stale card; applying the card is the user's explicit override."""
import asyncio
import json

from durin.agent.skill_suggestions import apply_suggestion, read_suggestions
from durin.agent.tools.skill_write import SkillWriteTool

NARRATION_BODY = """---
name: gadget-research
description: Research gadget opinions across the web. Use for gadget questions.
---
# Gadget Research

1. Run 3-6 web searches across forums, blogs, and review sites.
2. Fetch the top results and synthesize a cited summary.
"""

DELEGATING_BODY = """---
name: gadget-research
description: Research gadget opinions across the web. Use for gadget questions.
---
# Gadget Research

Run the `research-to-answer` workflow via run_workflow with the gadget question.
"""


def _reject_judge(prompt: str) -> str:
    return "Manual fan-out narration.\nNARRATION — delegate to a research workflow"


def _run(tool, **kwargs):
    return json.loads(asyncio.run(tool.execute(**kwargs)))


def test_hard_gate_bounce_lands_annotated_in_the_bandeja(tmp_path):
    tool = SkillWriteTool(workspace=tmp_path, gate_mode="hard",
                          composition_judge=_reject_judge)
    out = _run(tool, name="gadget-research", content=NARRATION_BODY, rationale="r")
    assert out["composition_rejected"] is True
    assert "bandeja" in out["note"]

    recs = read_suggestions(tmp_path)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["type"] == "create" and rec["skill"] == "gadget-research"
    assert "delegate to a research workflow" in rec["action"]["gate_reason"]
    assert "your word overrides the gate" in rec["reason"]
    assert rec["patch"]                                    # full body visible as a diff


def test_session_override_door_does_not_queue(tmp_path):
    tool = SkillWriteTool(workspace=tmp_path, composition_judge=_reject_judge)
    out = _run(tool, name="gadget-research", content=NARRATION_BODY, rationale="r")
    assert out["composition_rejected"] is True
    assert read_suggestions(tmp_path) == []                # the user is present; no card


def test_compliant_landing_clears_the_stale_card(tmp_path):
    def judge(prompt):
        body = prompt.split("Skill body to review:")[-1]
        return ("ok\nCOMPLIANT" if "run_workflow" in body
                else "narration\nNARRATION — delegate it")
    tool = SkillWriteTool(workspace=tmp_path, gate_mode="hard", composition_judge=judge)
    _run(tool, name="gadget-research", content=NARRATION_BODY, rationale="r")
    assert len(read_suggestions(tmp_path)) == 1
    out = _run(tool, name="gadget-research", content=DELEGATING_BODY, rationale="r")
    assert out.get("ok") is True
    assert read_suggestions(tmp_path) == []                # stale bounce cleared


def test_applying_the_card_creates_with_user_override(tmp_path):
    tool = SkillWriteTool(workspace=tmp_path, gate_mode="hard",
                          composition_judge=_reject_judge)
    _run(tool, name="gadget-research", content=NARRATION_BODY, rationale="r")
    rec = read_suggestions(tmp_path)[0]
    result = apply_suggestion(tmp_path, rec["action"])
    assert result.get("ok") is True                        # user's word wins
    assert (tmp_path / "skills" / "gadget-research" / "SKILL.md").is_file()
