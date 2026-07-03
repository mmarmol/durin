"""The composition gate at the skill-store boundary: narration-only bodies are
rejected with the judge's reason; judgment bodies pass; the gate is
failure-open; in-session the user's explicit word overrides, the dream's hard
mode does not. Fixtures are synthetic — no real-skill material."""
import asyncio
import json

from durin.agent.skills_doctrine import judge_composition
from durin.agent.skills_store import dream_create_skill
from durin.agent.tools.skill_write import SkillWriteTool

# A generic narration-only research procedure (the shape the gate exists to catch).
NARRATION_BODY = """---
name: gadget-research
description: Research gadget opinions across the web. Use for gadget questions.
---
# Gadget Research

1. Run 3-6 web searches across forums, blogs, and review sites.
2. Fetch the top results from each source type.
3. Extract the recommendations and their reasoning.
4. Synthesize a cited summary of the community consensus.
"""

# Judgment-only guidance — exactly what the doctrine reserves prose for.
JUDGMENT_BODY = """---
name: naming-conventions
description: How to name things in this codebase. Use when naming modules or APIs.
---
# Naming Conventions

Prefer domain words over mechanism words. A name earns an abbreviation only
when the long form appears more than five times per screen.
"""


def _reject_judge(prompt: str) -> str:
    return "It walks through fan-out searching manually.\nNARRATION — steps 1-4 should delegate to a research workflow"


def _accept_judge(prompt: str) -> str:
    return "Knowledge and judgment only.\nCOMPLIANT"


def test_narration_rejected_with_reason(tmp_path):
    out = dream_create_skill(tmp_path, "gadget-research", NARRATION_BODY, "r",
                             composition_judge=_reject_judge)
    assert out["composition_rejected"] is True
    assert "delegate to a research workflow" in out["error"]
    assert not (tmp_path / "skills" / "gadget-research").exists()


def test_judgment_body_accepted(tmp_path):
    out = dream_create_skill(tmp_path, "naming-conventions", JUDGMENT_BODY, "r",
                             composition_judge=_accept_judge)
    assert out.get("ok") is True


def test_gate_is_failure_open(tmp_path):
    def boom(prompt):
        raise RuntimeError("judge model unreachable")
    ok, reason = judge_composition("body", tmp_path, boom)
    assert ok is True
    ok, reason = judge_composition("body", tmp_path, lambda p: "some prose with no label")
    assert ok is True
    ok, reason = judge_composition("body", tmp_path, None)
    assert ok is True


def test_gate_prompt_carries_doctrine_catalog_and_body(tmp_path):
    seen = {}
    def spy(prompt):
        seen["prompt"] = prompt
        return "COMPLIANT"
    d = tmp_path / "workflows"
    d.mkdir()
    (d / "research-to-answer.json").write_text(json.dumps({
        "name": "research-to-answer", "description": "fan out and synthesize",
        "start": "only",
        "nodes": [{"id": "only", "title": "t", "kind": "work",
                   "mode": "read", "tools": "none", "prompt": "p"}],
    }), encoding="utf-8")
    judge_composition(NARRATION_BODY, tmp_path, spy)
    assert "research-to-answer" in seen["prompt"]
    assert "Gadget Research" in seen["prompt"]
    assert "is a skill even the right tool" in seen["prompt"]


def _run_tool(tool, **kwargs):
    return json.loads(asyncio.run(tool.execute(**kwargs)))


def test_session_override_wins_after_rejection(tmp_path):
    tool = SkillWriteTool(workspace=tmp_path, composition_judge=_reject_judge)
    out = _run_tool(tool, name="gadget-research", content=NARRATION_BODY, rationale="r")
    assert out["composition_rejected"] is True
    assert "override_composition" in out["hint"]        # the escape is told to the agent

    out = _run_tool(tool, name="gadget-research", content=NARRATION_BODY, rationale="r",
                    override_composition=True)
    assert out.get("ok") is True                        # the user's word wins


def test_dream_hard_mode_ignores_override(tmp_path):
    tool = SkillWriteTool(workspace=tmp_path, gate_mode="hard",
                          composition_judge=_reject_judge)
    out = _run_tool(tool, name="gadget-research", content=NARRATION_BODY, rationale="r",
                    override_composition=True)
    assert out["composition_rejected"] is True
    assert "override_composition" not in out.get("hint", "")   # no escape offered
    assert not (tmp_path / "skills" / "gadget-research").exists()
