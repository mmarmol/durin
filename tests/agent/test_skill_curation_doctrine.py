"""The curation judge prompt carries the composition doctrine and the workflow
catalog, so `evolve` can restructure narration-only skills into delegating wrappers."""
import json

from durin.agent.skill_curation import _build_prompt
from durin.agent.skills_doctrine import DOCTRINE_HEADING


def _workflow(ws, name="research-to-answer", description="fan out searches, synthesize"):
    d = ws / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({
        "name": name, "description": description, "start": "only",
        "nodes": [{"id": "only", "title": "Only", "kind": "work",
                   "mode": "read", "tools": "none", "prompt": "Answer."}],
    }), encoding="utf-8")


def test_prompt_embeds_doctrine_and_workspace_catalog(tmp_path):
    _workflow(tmp_path)
    out = _build_prompt({"a": "body"}, {}, workspace=tmp_path)
    assert DOCTRINE_HEADING in out                     # verbatim doctrine, single source
    assert "research-to-answer" in out                 # what a wrapper can delegate to
    assert "run_workflow" in out                       # the delegation instruction
    assert "norm violation fix" in out                 # licensed like English normalization


def test_prompt_without_workspace_stays_renderable(tmp_path):
    out = _build_prompt({"a": "body"}, {})
    assert DOCTRINE_HEADING in out
    assert "(no workflows installed)" in out
