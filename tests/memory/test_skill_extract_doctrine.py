"""The skill-extract pass carries the composition doctrine and the workflow
catalog, and its subagent can see and author workflows."""
import json

from durin.agent.skills_doctrine import DOCTRINE_HEADING
from durin.memory.dream_passes import (
    _build_skill_extract_tools,
    _skill_extract_messages,
)


def _session(ws, key, text="USER said something"):
    sdir = ws / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"{key}.jsonl").write_text(
        json.dumps({"role": "user", "content": text}) + "\n", encoding="utf-8")


def _workflow(ws, name="research-to-answer", description="fan out searches, synthesize"):
    d = ws / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({
        "name": name, "description": description, "start": "only",
        "nodes": [{"id": "only", "title": "Only", "kind": "work",
                   "mode": "read", "tools": "none", "prompt": "Answer."}],
    }), encoding="utf-8")


def test_system_prompt_embeds_doctrine_and_workflow_catalog(tmp_path):
    _session(tmp_path, "s1")
    _workflow(tmp_path)
    system = _skill_extract_messages(tmp_path, max_sessions=3)[0]["content"]
    assert DOCTRINE_HEADING in system                      # verbatim, not a paraphrase
    assert "research-to-answer" in system                  # the catalog names what exists
    assert "fan out searches, synthesize" in system
    assert "workflow_write" in system                      # the authoring escape hatch is named


def test_empty_catalog_is_explicit_not_missing(tmp_path):
    _session(tmp_path, "s1")
    system = _skill_extract_messages(tmp_path, max_sessions=3)[0]["content"]
    assert "(no workflows installed)" in system


def test_extract_toolset_can_see_and_author_workflows(tmp_path):
    tools = _build_skill_extract_tools(tmp_path, fs=None)
    names = set(tools.tool_names)
    assert "list_workflows" in names
    assert "workflow_write" in names
    assert "skill_write" in names                          # authoring path unchanged
