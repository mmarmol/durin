"""Regression coverage for the skill-extract dream's ``skills_touched``
metric and its ``gaps_closed`` telemetry field.

``skills_touched`` must count genuine authorings — a ``skill_write`` call
that actually landed a skill — not every ``skill_write`` tool invocation
regardless of outcome. A composition-rejected call still shows up in
``result.tool_events`` under the same tool name with a runner-level
``status: "ok"`` (the call itself didn't raise or return an ``Error``-
prefixed string), so counting by tool name alone over-counts.
"""
import json
from unittest.mock import AsyncMock, MagicMock

from durin.memory.dream_passes import run_skill_extract_pass
from durin.providers.base import LLMResponse, ToolCallRequest
from durin.telemetry.schema import MemoryDreamSkillExtractEvent


def test_gaps_closed_is_registered_field():
    assert "gaps_closed" in MemoryDreamSkillExtractEvent.__annotations__


NARRATION_BODY = """---
name: gadget-research
description: Research gadget opinions across the web. Use for gadget questions.
---
# Gadget Research

1. Run 3-6 web searches across forums, blogs, and review sites.
2. Fetch the top results from each source type.
3. Synthesize a cited summary of the community consensus.
"""

DELEGATING_BODY = """---
name: gadget-research
description: Research gadget opinions across the web. Use for gadget questions.
---
# Gadget Research

Gadget questions want community sources: enthusiast forums, review blogs, Reddit.

Run the `research-to-answer` workflow via run_workflow, with a task naming the
gadget, the angles above, and a request for cited community consensus.
"""


def _session(ws, key, text):
    sdir = ws / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"{key}.jsonl").write_text(
        json.dumps({"role": "user", "content": text}) + "\n", encoding="utf-8")


def _workflow(ws):
    d = ws / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "research-to-answer.json").write_text(json.dumps({
        "name": "research-to-answer", "description": "fan out searches, synthesize",
        "start": "only",
        "nodes": [{"id": "only", "title": "t", "kind": "work",
                   "mode": "read", "tools": "none", "prompt": "p"}],
    }), encoding="utf-8")


def _skill_write_call(i, body):
    return ToolCallRequest(id=f"call_{i}", name="skill_write", arguments={
        "name": "gadget-research", "content": body, "rationale": "recurring gadget research",
        # The dream tool runs gate_mode="hard": this override MUST be ignored.
        "override_composition": True,
    })


def _fake_judge(prompt: str, **kwargs):
    from durin.memory.llm_invoke import LLMResponse as AuxResponse
    if "run_workflow" in prompt.split("Skill body to review:")[-1]:
        return AuxResponse(text="Delegates properly.\nCOMPLIANT")
    return AuxResponse(text="Manual fan-out narration.\nNARRATION — delegate steps 1-3 to research-to-answer")


def test_skills_touched_counts_landed_not_rejected(tmp_path, monkeypatch):
    """One skill_write call is composition-rejected, one lands. Both produce
    a ``skill_write`` tool_event with runner ``status: "ok"`` — skills_touched
    must count only the one that actually landed."""
    from durin.memory import llm_invoke
    monkeypatch.setattr(llm_invoke, "judge_llm_invoke", _fake_judge)

    _session(tmp_path, "s1", "user researched gadget opinions across many sites")
    _workflow(tmp_path)

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(content="", tool_calls=[_skill_write_call(0, NARRATION_BODY)],
                    finish_reason="tool_calls", usage={}),
        LLMResponse(content="", tool_calls=[_skill_write_call(1, DELEGATING_BODY)],
                    finish_reason="tool_calls", usage={}),
        LLMResponse(content="done", tool_calls=[], finish_reason="stop", usage={}),
    ])

    out = run_skill_extract_pass(tmp_path, provider=provider, model="test-model")
    assert not out.get("error"), out

    md = tmp_path / "skills" / "gadget-research" / "SKILL.md"
    assert md.is_file()  # the delegating retry actually landed on disk

    assert out["skills_touched"] == 1
