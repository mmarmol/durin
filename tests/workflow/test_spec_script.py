import pytest

from durin.workflow.spec import ScriptNode, WorkflowError, node_label, parse_workflow


def _wf(nodes, start="s"):
    return {"name": "t", "start": start, "nodes": nodes}


def test_parse_minimal_command_node():
    wf = parse_workflow(_wf([{"id": "s", "kind": "script", "command": "pytest -q"}]))
    node = wf.nodes["s"]
    assert isinstance(node, ScriptNode)
    assert node.command == "pytest -q" and node.script == ""
    assert node.timeout is None and node.next is None and not node.routes


def test_parse_script_file_node_with_routing():
    wf = parse_workflow(_wf([
        {"id": "s", "kind": "script", "script": "check.py", "on_pass": None, "on_fail": "s"},
    ]))
    assert wf.nodes["s"].script == "check.py" and wf.nodes["s"].routes


def test_command_xor_script():
    with pytest.raises(WorkflowError, match="exactly one"):
        parse_workflow(_wf([{"id": "s", "kind": "script"}]))
    with pytest.raises(WorkflowError, match="exactly one"):
        parse_workflow(_wf([{"id": "s", "kind": "script", "command": "x", "script": "y.sh"}]))


def test_agent_only_fields_rejected():
    for field in ("model", "persona", "context", "session", "prompt", "mode", "tools", "skills", "mcps", "max_turns"):
        with pytest.raises(WorkflowError, match="do not apply"):
            parse_workflow(_wf([{"id": "s", "kind": "script", "command": "x", field: "v"}]))


def test_script_path_must_stay_inside_scripts_dir():
    for bad in ("/abs/path.sh", "../escape.sh", "a/../../b.sh"):
        with pytest.raises(WorkflowError, match="relative path"):
            parse_workflow(_wf([{"id": "s", "kind": "script", "script": bad}]))


def test_routing_exclusive_and_cases_validated():
    with pytest.raises(WorkflowError, match="mutually exclusive"):
        parse_workflow(_wf([{"id": "s", "kind": "script", "command": "x",
                             "next": "s", "on_pass": None, "on_fail": "s"}]))
    with pytest.raises(WorkflowError, match="normalize to the same form"):
        parse_workflow(_wf([{"id": "s", "kind": "script", "command": "x",
                             "cases": {"OK": None, "ok.": "s"}}]))


def test_cases_may_target_needs_input():
    wf = parse_workflow(_wf([{"id": "s", "kind": "script", "command": "x",
                              "cases": {"READY": None, "MISSING": "__needs_input__"}}]))
    assert wf.nodes["s"].cases["MISSING"] == "__needs_input__"


def test_timeout_and_max_visits_validated():
    with pytest.raises(WorkflowError, match="timeout"):
        parse_workflow(_wf([{"id": "s", "kind": "script", "command": "x", "timeout": 0}]))
    wf = parse_workflow(_wf([{"id": "s", "kind": "script", "command": "x", "timeout": 30, "max_visits": 2}]))
    assert wf.nodes["s"].timeout == 30 and wf.nodes["s"].max_visits == 2


def test_env_defaults_to_clean():
    wf = parse_workflow(_wf([{"id": "s", "kind": "script", "command": "x"}]))
    assert wf.nodes["s"].env == "clean"


def test_env_accepts_inherit():
    wf = parse_workflow(_wf([{"id": "s", "kind": "script", "command": "x", "env": "inherit"}]))
    assert wf.nodes["s"].env == "inherit"


def test_env_rejects_other_values():
    with pytest.raises(WorkflowError, match="env"):
        parse_workflow(_wf([{"id": "s", "kind": "script", "command": "x", "env": "full"}]))


def test_script_node_accepted_as_branch_but_not_as_worker():
    # A script may run BESIDE agent branches (deterministic fetch ∥ LLM analysis).
    wf = parse_workflow(_wf([
        {"id": "s", "kind": "parallel", "branches": ["b"], "next": None},
        {"id": "b", "kind": "script", "command": "x"},
    ]))
    assert wf.nodes["s"].branches == ("b",)
    # The dynamic worker template stays agent-only: a script iterates over a list
    # internally in one execution, so mapping one subprocess per item adds nothing.
    with pytest.raises(WorkflowError, match="must be a work node"):
        parse_workflow(_wf([
            {"id": "s", "kind": "parallel", "worker": "w", "list_from": "s", "next": None},
            {"id": "w", "kind": "script", "command": "x"},
        ]))


def test_edge_targets_validated():
    with pytest.raises(WorkflowError, match="unknown node"):
        parse_workflow(_wf([{"id": "s", "kind": "script", "command": "x", "next": "ghost"}]))


def test_node_label_falls_back_to_command_then_file():
    assert node_label(ScriptNode(id="n", command="pytest -q")) == "pytest -q"
    assert node_label(ScriptNode(id="n", script="check.py")) == "check.py"
    assert node_label(ScriptNode(id="n", title="Gate", command="x")) == "Gate"


def test_script_path_aliases_normalize_to_canonical():
    wf = parse_workflow(_wf([{"id": "s", "kind": "script", "script": "./check.py"}]))
    assert wf.nodes["s"].script == "check.py"
    wf2 = parse_workflow(_wf([{"id": "s", "kind": "script", "script": "sub/./tool.sh"}]))
    assert wf2.nodes["s"].script == "sub/tool.sh"


def test_script_node_secrets_parse():
    wf = parse_workflow(_wf([{
        "id": "s", "kind": "script", "command": "true",
        "secrets": ["ZENDESK_API_TOKEN", "MXHERO_KEY"],
    }]))
    assert wf.nodes["s"].secrets == ("ZENDESK_API_TOKEN", "MXHERO_KEY")


def test_script_node_secrets_default_empty():
    wf = parse_workflow(_wf([{"id": "s", "kind": "script", "command": "true"}]))
    assert wf.nodes["s"].secrets == ()


def test_script_node_secrets_rejects_non_list():
    with pytest.raises(WorkflowError, match="secrets"):
        parse_workflow(_wf([{
            "id": "s", "kind": "script", "command": "true",
            "secrets": "ZENDESK_API_TOKEN",
        }]))


def test_script_node_secrets_rejects_invalid_name():
    with pytest.raises(WorkflowError, match="env-var-safe"):
        parse_workflow(_wf([{
            "id": "s", "kind": "script", "command": "true",
            "secrets": ["lower-case"],
        }]))
