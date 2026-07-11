"""Dream sub-agents that hold `workflow_write` cannot read the packaged
`workflows` skill with their scoped file tools, so the prompts that offer
workflow authoring must EMBED the authoring schema. These tests pin that the
helper returns the real packaged reference (script node included) and that
both authoring prompts actually carry it.
"""

from __future__ import annotations

from durin.agent.skills_doctrine import workflow_authoring_reference


def test_reference_is_the_packaged_schema_with_script_nodes():
    ref = workflow_authoring_reference()
    assert "## Node kinds" in ref
    assert '`script`' in ref and "deterministic subprocess" in ref
    assert "on_pass" in ref and "cases" in ref


def test_restructure_prompt_embeds_the_schema():
    from durin.agent.skill_restructure import _RESTRUCTURE_PROMPT
    assert "{workflow_authoring}" in _RESTRUCTURE_PROMPT
    rendered = _RESTRUCTURE_PROMPT.format(
        doctrine="D", workflow_catalog="C",
        workflow_authoring=workflow_authoring_reference(),
        name="x", current="body", intent="i",
    )
    assert "deterministic subprocess" in rendered and '`script`' in rendered


def test_skill_extract_prompt_embeds_the_schema():
    from durin.memory.dream_passes import _SKILL_EXTRACT_PROMPT
    assert "{workflow_authoring}" in _SKILL_EXTRACT_PROMPT
    rendered = _SKILL_EXTRACT_PROMPT.format(
        doctrine="D", workflow_catalog="C",
        workflow_authoring=workflow_authoring_reference(),
        existing="(none)", principles="",
    )
    assert "deterministic subprocess" in rendered and '`script`' in rendered
