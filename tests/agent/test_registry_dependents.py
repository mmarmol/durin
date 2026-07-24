"""Who references this? — the question nothing in the system could answer.

Workflow work nodes name skills, script nodes name a file under
workflows/scripts/, sub-flow nodes name another workflow, and a loop names the
workflow it runs. Those edges are computable, but no code consulted them, so the
dream could fuse away a skill a workflow depends on and leave the reference
dangling.
"""

import json

from durin.registry_graph import dependents_of


def _workflow(ws, name, nodes):
    d = ws / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(
        {"name": name, "start": nodes[0]["id"], "nodes": nodes}))


def _loop(ws, name, workflow):
    d = ws / "loops"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(
        {"name": name, "workflow": workflow, "goal": {"intent": "x"}}))


def test_finds_the_workflow_node_that_names_a_skill(tmp_path):
    _workflow(tmp_path, "triage", [{"id": "a", "kind": "work", "skills": ["mxhero-support-api"]}])

    deps = dependents_of(tmp_path, skill="mxhero-support-api")

    assert [(d.kind, d.name, d.via, d.where) for d in deps] == [
        ("workflow", "triage", "skills", "a")]


def test_finds_the_workflow_node_that_runs_a_script(tmp_path):
    _workflow(tmp_path, "triage", [{"id": "s", "kind": "script", "script": "resolve-org.py"}])

    deps = dependents_of(tmp_path, script="resolve-org.py")

    assert [(d.kind, d.name, d.via) for d in deps] == [("workflow", "triage", "script")]


def test_finds_the_subflow_caller_and_the_loop(tmp_path):
    _workflow(tmp_path, "child", [{"id": "a", "kind": "work"}])
    _workflow(tmp_path, "parent", [{"id": "s", "kind": "subworkflow", "workflow": "child"}])
    _loop(tmp_path, "nightly", "child")

    deps = dependents_of(tmp_path, workflow="child")

    assert {(d.kind, d.name, d.via) for d in deps} == {
        ("workflow", "parent", "subworkflow"), ("loop", "nightly", "workflow")}


def test_an_unreferenced_artifact_has_no_dependents(tmp_path):
    _workflow(tmp_path, "triage", [{"id": "a", "kind": "work", "skills": ["other"]}])

    assert dependents_of(tmp_path, skill="mxhero-support-api") == []


def test_a_malformed_definition_never_breaks_the_query(tmp_path):
    """A broken file must not make the barrier fail open OR blow up."""
    _workflow(tmp_path, "good", [{"id": "a", "kind": "work", "skills": ["s"]}])
    (tmp_path / "workflows" / "broken.json").write_text("{not json")

    deps = dependents_of(tmp_path, skill="s")

    assert [d.name for d in deps] == ["good"]


def test_missing_directories_are_not_an_error(tmp_path):
    assert dependents_of(tmp_path, skill="anything") == []
