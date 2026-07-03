"""The composition doctrine is loaded verbatim from the skill-creator builtin,
and the workflow catalog renders the workspace's definitions for prompts."""
import json

from durin.agent.skills_doctrine import (
    DOCTRINE_HEADING,
    composition_doctrine,
    workflow_catalog_text,
)


def test_doctrine_heading_exists_in_builtin():
    # Pinned on purpose: renaming the section in skill-creator/SKILL.md must
    # break here so the constant and the skill move together.
    from durin.agent.skills import BUILTIN_SKILLS_DIR

    text = (BUILTIN_SKILLS_DIR / "skill-creator" / "SKILL.md").read_text(encoding="utf-8")
    assert DOCTRINE_HEADING in text


def test_doctrine_section_extracted_verbatim():
    section = composition_doctrine()
    assert section.startswith(DOCTRINE_HEADING)
    # The three mechanisms and the composition rule are all present.
    for needle in ("script", "workflow", "skill", "compose"):
        assert needle in section.lower()
    # Extraction stops at the next section — Core Principles is not doctrine.
    assert "## Core Principles" not in section


def _write_workflow(workspace, name, description):
    d = workspace / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({
        "name": name,
        "description": description,
        "start": "only",
        "input": {"text": True, "description": "a question"},
        "output": {"text": True, "description": "an answer"},
        "nodes": [{"id": "only", "title": "Only", "kind": "work",
                   "mode": "read", "tools": "none", "prompt": "Answer."}],
    }), encoding="utf-8")


def test_workflow_catalog_lists_definitions(tmp_path):
    _write_workflow(tmp_path, "beta", "second one")
    _write_workflow(tmp_path, "alpha", "first one")
    text = workflow_catalog_text(tmp_path)
    assert "- alpha — first one" in text
    assert "- beta — second one" in text
    assert "input: a question" in text and "output: an answer" in text
    # Stable (sorted) ordering.
    assert text.index("alpha") < text.index("beta")


def test_workflow_catalog_skips_malformed_and_handles_empty(tmp_path):
    assert workflow_catalog_text(tmp_path) == "(no workflows installed)"
    d = tmp_path / "workflows"
    d.mkdir()
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    assert workflow_catalog_text(tmp_path) == "(no workflows installed)"
