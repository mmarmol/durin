"""Dream-side E2E: the skill-extract sub-agent cannot land a narration-only
skill — the composition gate bounces it with the reason, and the retry that
delegates to the existing workflow lands. Exercises the real pass runner, the
real toolset (hard gate), and the real store; the LLM and the gate judge are
scripted fakes."""
import json
from unittest.mock import AsyncMock, MagicMock

from durin.memory.dream_passes import run_skill_extract_pass
from durin.providers.base import LLMResponse, ToolCallRequest

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


def test_narration_bounces_then_delegating_retry_lands(tmp_path, monkeypatch):
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
    assert not out.get("error")

    md = tmp_path / "skills" / "gadget-research" / "SKILL.md"
    assert md.is_file()
    body = md.read_text(encoding="utf-8")
    assert "run_workflow" in body                        # the delegating retry landed
    assert "Run 3-6 web searches" not in body            # the narration never did
