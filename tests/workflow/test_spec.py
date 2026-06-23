"""Tests for parsing a workflow JSON definition."""

import pytest

from durin.workflow.spec import (
    DecisionNode,
    WorkNode,
    Workflow,
    WorkflowError,
    parse_workflow,
)

_VALID = {
    "name": "demo",
    "start": "build",
    "max_visits": 2,
    "nodes": [
        {"id": "build", "kind": "work", "model": "fast", "context": "own",
         "prompt": "Write the code.", "next": "check"},
        {"id": "check", "kind": "decision", "command": "true",
         "on_pass": None, "on_fail": "build"},
    ],
}


def test_parse_valid_workflow():
    wf = parse_workflow(_VALID)
    assert wf.name == "demo"
    assert wf.start == "build"
    assert wf.max_visits == 2
    assert isinstance(wf.nodes["build"], WorkNode)
    assert wf.nodes["build"].model == "fast"
    assert wf.nodes["build"].context == "own"
    assert wf.nodes["build"].next == "check"
    assert isinstance(wf.nodes["check"], DecisionNode)
    assert wf.nodes["check"].command == "true"
    assert wf.nodes["check"].on_pass is None
    assert wf.nodes["check"].on_fail == "build"


def test_work_node_defaults():
    wf = parse_workflow({"name": "d", "start": "a",
                         "nodes": [{"id": "a", "kind": "work"}]})
    a = wf.nodes["a"]
    assert a.model is None        # None = engine default model
    assert a.context == "own"     # default context
    assert a.prompt == ""
    assert a.next is None         # None = end
    assert wf.max_visits == 3     # default loop cap


def test_work_node_skills_and_mcps_default_empty():
    a = parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "work"}]}).nodes["a"]
    assert a.skills == ()
    assert a.mcps == ()


def test_work_node_parses_skills_and_mcps():
    a = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "skills": ["pdf-extract"], "mcps": ["github-mcp-server"]},
    ]}).nodes["a"]
    assert a.skills == ("pdf-extract",)
    assert a.mcps == ("github-mcp-server",)


def test_work_node_mode_defaults_build_and_parses():
    a = parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "work"}]}).nodes["a"]
    assert a.mode == "build"
    b = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "mode": "plan"}]}).nodes["a"]
    assert b.mode == "plan"


def test_work_node_mode_must_be_a_string():
    with pytest.raises(WorkflowError, match="mode must be"):
        parse_workflow({"name": "d", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "mode": 123}]})


def test_work_node_skills_must_be_string_list():
    with pytest.raises(WorkflowError, match="skills must be a list of strings"):
        parse_workflow({"name": "d", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "skills": "pdf-extract"},
        ]})
    with pytest.raises(WorkflowError, match="mcps must be a list of strings"):
        parse_workflow({"name": "d", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "mcps": [123]},
        ]})


def test_parallel_reconcile_defaults_to_read():
    fan = parse_workflow({"name": "d", "start": "fan", "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["a"], "next": None},
        {"id": "a", "kind": "work"},
    ]}).nodes["fan"]
    assert fan.reconcile == "read"


def test_parallel_choose_requires_criteria():
    with pytest.raises(WorkflowError, match="needs 'criteria'"):
        parse_workflow({"name": "d", "start": "fan", "nodes": [
            {"id": "fan", "kind": "parallel", "branches": ["a"], "reconcile": "choose", "next": None},
            {"id": "a", "kind": "work"},
        ]})


def test_parallel_invalid_reconcile_raises():
    with pytest.raises(WorkflowError, match="reconcile must be"):
        parse_workflow({"name": "d", "start": "fan", "nodes": [
            {"id": "fan", "kind": "parallel", "branches": ["a"], "reconcile": "merge", "next": None},
            {"id": "a", "kind": "work"},
        ]})


def test_parallel_union_parses():
    fan = parse_workflow({"name": "d", "start": "fan", "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["a", "b"], "reconcile": "union", "next": None},
        {"id": "a", "kind": "work"}, {"id": "b", "kind": "work"},
    ]}).nodes["fan"]
    assert fan.reconcile == "union"


def test_improvement_mode_defaults_off_and_parses():
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [{"id": "a", "kind": "work"}]})
    assert wf.improvement_mode == "off"
    wf2 = parse_workflow({"name": "d", "start": "a", "improvement_mode": "manual",
                          "nodes": [{"id": "a", "kind": "work"}]})
    assert wf2.improvement_mode == "manual"


def test_invalid_improvement_mode_raises():
    with pytest.raises(WorkflowError, match="improvement_mode must be"):
        parse_workflow({"name": "d", "start": "a", "improvement_mode": "yes",
                        "nodes": [{"id": "a", "kind": "work"}]})


def test_unknown_start_raises():
    with pytest.raises(WorkflowError, match="start"):
        parse_workflow({"name": "d", "start": "missing",
                        "nodes": [{"id": "a", "kind": "work"}]})


def test_edge_to_unknown_node_raises():
    with pytest.raises(WorkflowError, match="unknown node"):
        parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "next": "ghost"}]})


def test_unknown_kind_raises():
    with pytest.raises(WorkflowError, match="kind"):
        parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "magic"}]})


def test_invalid_context_raises():
    with pytest.raises(WorkflowError, match="context"):
        parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "context": "sideways"}]})


def test_missing_name_raises():
    with pytest.raises(WorkflowError, match="name"):
        parse_workflow({"start": "a", "nodes": [{"id": "a", "kind": "work"}]})


def test_missing_start_raises():
    with pytest.raises(WorkflowError, match="start"):
        parse_workflow({"name": "d", "nodes": [{"id": "a", "kind": "work"}]})


def test_duplicate_node_id_raises():
    with pytest.raises(WorkflowError, match="duplicate"):
        parse_workflow({"name": "d", "start": "a", "nodes": [
            {"id": "a", "kind": "work"}, {"id": "a", "kind": "work"}]})


def test_work_node_tools_default_is_none():
    wf = parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "work"}]})
    assert wf.nodes["a"].tools == "none"


def test_work_node_parses_tools_default_value():
    wf = parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "tools": "default"}]})
    assert wf.nodes["a"].tools == "default"


def test_invalid_tools_raises():
    with pytest.raises(WorkflowError, match="tools"):
        parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "tools": "everything"}]})


def test_zero_max_visits_raises():
    with pytest.raises(WorkflowError, match="max_visits"):
        parse_workflow({"name": "d", "start": "a", "max_visits": 0,
                        "nodes": [{"id": "a", "kind": "work"}]})


def test_non_int_max_visits_raises():
    with pytest.raises(WorkflowError, match="max_visits"):
        parse_workflow({"name": "d", "start": "a", "max_visits": "lots",
                        "nodes": [{"id": "a", "kind": "work"}]})


def test_non_string_model_raises():
    with pytest.raises(WorkflowError, match="model"):
        parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "model": 123}]})


def test_decision_node_parses_criteria_and_judge_model():
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "g"},
        {"id": "g", "kind": "decision", "criteria": "Is it correct?",
         "judge_model": "deep", "on_pass": None, "on_fail": "a"},
    ]})
    g = wf.nodes["g"]
    assert g.criteria == "Is it correct?"
    assert g.judge_model == "deep"
    assert g.command == ""


def test_decision_with_both_command_and_criteria_raises():
    with pytest.raises(WorkflowError, match="exactly one"):
        parse_workflow({"name": "d", "start": "g", "nodes": [
            {"id": "g", "kind": "decision", "command": "true",
             "criteria": "ok?", "on_pass": None, "on_fail": None},
        ]})


def test_decision_with_neither_command_nor_criteria_raises():
    with pytest.raises(WorkflowError, match="exactly one"):
        parse_workflow({"name": "d", "start": "g", "nodes": [
            {"id": "g", "kind": "decision", "on_pass": None, "on_fail": None},
        ]})


def test_parses_subworkflow_node():
    from durin.workflow.spec import SubworkflowNode
    wf = parse_workflow({"name": "d", "start": "sub", "nodes": [
        {"id": "sub", "kind": "subworkflow", "workflow": "reviewer", "next": None},
    ]})
    n = wf.nodes["sub"]
    assert isinstance(n, SubworkflowNode)
    assert n.workflow == "reviewer"
    assert n.next is None


def test_subworkflow_without_workflow_name_raises():
    with pytest.raises(WorkflowError, match="workflow"):
        parse_workflow({"name": "d", "start": "sub", "nodes": [
            {"id": "sub", "kind": "subworkflow", "next": None},
        ]})


def test_subworkflow_edge_target_validated():
    with pytest.raises(WorkflowError, match="unknown node"):
        parse_workflow({"name": "d", "start": "sub", "nodes": [
            {"id": "sub", "kind": "subworkflow", "workflow": "x", "next": "ghost"},
        ]})


def test_parses_parallel_node():
    from durin.workflow.spec import ParallelNode
    wf = parse_workflow({"name": "d", "start": "fan", "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["a", "b"], "next": "join"},
        {"id": "a", "kind": "work"},
        {"id": "b", "kind": "work"},
        {"id": "join", "kind": "work", "next": None},
    ]})
    n = wf.nodes["fan"]
    assert isinstance(n, ParallelNode)
    assert list(n.branches) == ["a", "b"]
    assert n.next == "join"


def test_parallel_without_branches_raises():
    with pytest.raises(WorkflowError, match="branches"):
        parse_workflow({"name": "d", "start": "fan", "nodes": [
            {"id": "fan", "kind": "parallel", "branches": [], "next": None},
        ]})


def test_parallel_branch_must_be_work_node():
    with pytest.raises(WorkflowError, match="work node"):
        parse_workflow({"name": "d", "start": "fan", "nodes": [
            {"id": "fan", "kind": "parallel", "branches": ["g"], "next": None},
            {"id": "g", "kind": "decision", "command": "true", "on_pass": None, "on_fail": None},
        ]})


def test_parallel_unknown_branch_raises():
    with pytest.raises(WorkflowError, match="unknown node"):
        parse_workflow({"name": "d", "start": "fan", "nodes": [
            {"id": "fan", "kind": "parallel", "branches": ["ghost"], "next": None},
        ]})
