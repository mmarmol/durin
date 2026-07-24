"""Tests for parsing a workflow JSON definition."""

import pytest

from durin.workflow.spec import (
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
        {"id": "check", "kind": "work", "prompt": "Is it correct?",
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
    # the routing node: prompt holds the verdict criterion
    assert isinstance(wf.nodes["check"], WorkNode)
    assert wf.nodes["check"].prompt == "Is it correct?"
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


def test_improvement_mode_defaults_manual_and_parses():
    # Two states only (auto/manual), mirroring a skill's self-improvement mode; default manual.
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [{"id": "a", "kind": "work"}]})
    assert wf.improvement_mode == "manual"
    wf2 = parse_workflow({"name": "d", "start": "a", "improvement_mode": "auto",
                          "nodes": [{"id": "a", "kind": "work"}]})
    assert wf2.improvement_mode == "auto"


def test_invalid_improvement_mode_raises():
    with pytest.raises(WorkflowError, match="improvement_mode must be"):
        parse_workflow({"name": "d", "start": "a", "improvement_mode": "yes",
                        "nodes": [{"id": "a", "kind": "work"}]})
    # "off" was removed (two states only) — it is now rejected, not silently accepted.
    with pytest.raises(WorkflowError, match="improvement_mode must be"):
        parse_workflow({"name": "d", "start": "a", "improvement_mode": "off",
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


def test_work_node_with_routing_is_a_routing_node():
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "prompt": "judge it", "on_pass": "b", "on_fail": "a"},
        {"id": "b", "kind": "work"},
    ]})
    a = wf.nodes["a"]
    assert isinstance(a, WorkNode) and a.routes
    assert a.on_pass == "b" and a.on_fail == "a"


def test_a_node_cannot_have_both_next_and_routing():
    with pytest.raises(WorkflowError):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "next": "b", "on_pass": "b"},
            {"id": "b", "kind": "work"},
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


def test_parallel_branch_must_be_work_or_script_node():
    with pytest.raises(WorkflowError, match="work or script node"):
        parse_workflow({"name": "d", "start": "fan", "nodes": [
            {"id": "fan", "kind": "parallel", "branches": ["sub"], "next": None},
            {"id": "sub", "kind": "subworkflow", "workflow": "inner"},
        ]})


def test_parallel_unknown_branch_raises():
    with pytest.raises(WorkflowError, match="unknown node"):
        parse_workflow({"name": "d", "start": "fan", "nodes": [
            {"id": "fan", "kind": "parallel", "branches": ["ghost"], "next": None},
        ]})


def _wf(nodes, start="a"):
    return {"name": "w", "start": start, "nodes": nodes}


def test_rejects_structurally_identical_producer_and_judge():
    with pytest.raises(WorkflowError):
        parse_workflow(_wf([
            {"id": "p", "kind": "work", "model": "m", "mode": "build", "prompt": "do x", "next": "j"},
            {"id": "j", "kind": "work", "model": "m", "mode": "build", "prompt": "do x",
             "on_pass": "done", "on_fail": "p"},
            {"id": "done", "kind": "work"},
        ], start="p"))


def test_allows_judge_differing_by_model_or_prompt():
    wf = parse_workflow(_wf([
        {"id": "p", "kind": "work", "model": "m", "prompt": "do x", "next": "j"},
        {"id": "j", "kind": "work", "model": "m", "mode": "explore", "prompt": "grade x",
         "on_pass": "done", "on_fail": "p"},
        {"id": "done", "kind": "work"},
    ], start="p"))
    assert wf.nodes["j"].routes      # differs by prompt+mode -> allowed


# ── Task 1 new tests ──────────────────────────────────────────────────────────

def test_workflow_io_descriptors_parse():
    wf = parse_workflow({"name": "w", "start": "a", "input": {"text": True, "file": True},
                         "output": {"file": True}, "nodes": [{"id": "a", "kind": "work"}]})
    assert wf.input == {"text": True, "file": True} and wf.output == {"file": True}


def test_workflow_io_defaults_none():
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [{"id": "a", "kind": "work"}]})
    assert wf.input is None and wf.output is None


def test_workflow_io_must_be_dict():
    with pytest.raises(WorkflowError):
        parse_workflow({"name": "w", "start": "a", "input": "text",
                        "nodes": [{"id": "a", "kind": "work"}]})
    with pytest.raises(WorkflowError):
        parse_workflow({"name": "w", "start": "a", "output": ["file"],
                        "nodes": [{"id": "a", "kind": "work"}]})


def test_node_persona_xor_model():
    a = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "persona": "engineer"}]}).nodes["a"]
    assert a.persona == "engineer"
    with pytest.raises(WorkflowError):     # both set → reject
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "persona": "engineer", "model": "glm-5.2"}]})


def test_node_persona_defaults_none():
    a = parse_workflow({"name": "w", "start": "a",
                        "nodes": [{"id": "a", "kind": "work"}]}).nodes["a"]
    assert a.persona is None


def test_parallel_max_concurrency_defaults_to_global_caps_and_parses():
    # Absent = None: the engine applies the global per-kind caps from config
    # (workflow.parallel_llm_concurrency / parallel_script_concurrency). An
    # explicit value is a uniform per-node override.
    fan = parse_workflow({"name": "w", "start": "f", "nodes": [
        {"id": "f", "kind": "parallel", "branches": ["a"], "next": None},
        {"id": "a", "kind": "work"}]}).nodes["f"]
    assert fan.max_concurrency is None
    fan2 = parse_workflow({"name": "w", "start": "f", "nodes": [
        {"id": "f", "kind": "parallel", "branches": ["a"], "max_concurrency": 5, "next": None},
        {"id": "a", "kind": "work"}]}).nodes["f"]
    assert fan2.max_concurrency == 5


def test_parallel_max_concurrency_must_be_at_least_1():
    with pytest.raises(WorkflowError):
        parse_workflow({"name": "w", "start": "f", "nodes": [
            {"id": "f", "kind": "parallel", "branches": ["a"], "max_concurrency": 0, "next": None},
            {"id": "a", "kind": "work"}]})


def test_dynamic_parallel_worker_and_list_from():
    wf = parse_workflow({"name": "w", "start": "orch", "nodes": [
        {"id": "orch", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "worker": "dev", "list_from": "orch", "next": "done"},
        {"id": "dev", "kind": "work"}, {"id": "done", "kind": "work"}]})
    fan = wf.nodes["fan"]
    assert fan.worker == "dev" and fan.list_from == "orch" and fan.branches == ()


def test_dynamic_parallel_branches_must_be_empty():
    with pytest.raises(WorkflowError):
        parse_workflow({"name": "w", "start": "orch", "nodes": [
            {"id": "orch", "kind": "work", "next": "fan"},
            {"id": "fan", "kind": "parallel", "branches": ["a"], "worker": "dev",
             "list_from": "orch", "next": None},
            {"id": "a", "kind": "work"}, {"id": "dev", "kind": "work"}]})


def test_dynamic_parallel_requires_list_from():
    with pytest.raises(WorkflowError):
        parse_workflow({"name": "w", "start": "orch", "nodes": [
            {"id": "orch", "kind": "work", "next": "fan"},
            {"id": "fan", "kind": "parallel", "worker": "dev", "next": None},
            {"id": "dev", "kind": "work"}]})


def test_dynamic_parallel_worker_and_list_from_must_be_real_nodes():
    with pytest.raises(WorkflowError):
        parse_workflow({"name": "w", "start": "orch", "nodes": [
            {"id": "orch", "kind": "work", "next": "fan"},
            {"id": "fan", "kind": "parallel", "worker": "ghost", "list_from": "orch", "next": None}]})


# ── cases: multi-way routing spec tests ──────────────────────────────────────


def _cases_wf(cases, extra_nodes=None):
    """Build a minimal workflow with a single cases node for parse tests."""
    nodes = [{"id": "a", "kind": "work", "cases": cases}]
    if extra_nodes:
        nodes.extend(extra_nodes)
    return {"name": "w", "start": "a", "nodes": nodes}


def test_cases_node_parses_and_cases_field_set():
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "cases": {"GROUNDED": None, "MISSING": "fix", "MISUSED": "fix"}},
        {"id": "fix", "kind": "work"},
    ]})
    a = wf.nodes["a"]
    assert isinstance(a, WorkNode)
    assert a.cases == {"GROUNDED": None, "MISSING": "fix", "MISUSED": "fix"}


def test_cases_node_routes_property_is_true():
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "cases": {"DONE": None, "RETRY": "a"}},
    ]})
    assert wf.nodes["a"].routes is True


def test_cases_node_mode_defaults_explore():
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "cases": {"DONE": None}},
    ]})
    assert wf.nodes["a"].mode == "explore"


def test_cases_and_next_are_mutually_exclusive():
    with pytest.raises(WorkflowError, match="mutually exclusive"):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "cases": {"DONE": None}, "next": "b"},
            {"id": "b", "kind": "work"},
        ]})


def test_cases_and_on_pass_are_mutually_exclusive():
    with pytest.raises(WorkflowError, match="mutually exclusive"):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "cases": {"DONE": None}, "on_pass": "b"},
            {"id": "b", "kind": "work"},
        ]})



def test_cases_empty_dict_raises():
    with pytest.raises(WorkflowError, match="must not be empty"):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "cases": {}},
        ]})


def test_cases_non_dict_raises():
    with pytest.raises(WorkflowError, match="must be a dict"):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "cases": ["DONE", "RETRY"]},
        ]})


def test_cases_unknown_target_caught_by_reachability():
    with pytest.raises(WorkflowError, match="unknown node"):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "cases": {"DONE": "ghost"}},
        ]})


def test_cases_null_target_is_valid():
    # null target = end the run; should parse without error.
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "cases": {"DONE": None, "RETRY": "a"}},
    ]})
    assert wf.nodes["a"].cases["DONE"] is None


def test_cases_default_label_is_valid():
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "cases": {"DONE": None, "default": "a"}},
    ]})
    assert "default" in wf.nodes["a"].cases


def test_cases_duplicate_normalized_labels_raise():
    # "DONE" and "done" both normalize to "DONE" — the spec must reject this to
    # prevent a silent mis-route (parse_label uses the same normalization).
    with pytest.raises(WorkflowError, match="normalize to the same form"):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "cases": {"DONE": None, "done": "a"}},
        ]})


# ── max_turns spec tests ──────────────────────────────────────────────────────


def test_max_turns_defaults_none():
    a = parse_workflow({"name": "w", "start": "a",
                        "nodes": [{"id": "a", "kind": "work"}]}).nodes["a"]
    assert a.max_turns is None


def test_max_turns_parses_valid_int():
    a = parse_workflow({"name": "w", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "max_turns": 6}]}).nodes["a"]
    assert a.max_turns == 6


def test_max_turns_zero_raises():
    with pytest.raises(WorkflowError, match="max_turns must be an int >= 1"):
        parse_workflow({"name": "w", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "max_turns": 0}]})


def test_max_turns_negative_raises():
    with pytest.raises(WorkflowError, match="max_turns must be an int >= 1"):
        parse_workflow({"name": "w", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "max_turns": -3}]})


def test_max_turns_bool_rejected():
    # bool is a subclass of int; True == 1 but must still be rejected.
    with pytest.raises(WorkflowError, match="max_turns must be an int >= 1"):
        parse_workflow({"name": "w", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "max_turns": True}]})


def test_max_turns_string_raises():
    with pytest.raises(WorkflowError, match="max_turns must be an int >= 1"):
        parse_workflow({"name": "w", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "max_turns": "6"}]})


# ── max_reentries / reentry_prompt spec tests ─────────────────────────────────


def test_reentry_fields_default_off():
    a = parse_workflow({"name": "w", "start": "a",
                        "nodes": [{"id": "a", "kind": "work"}]}).nodes["a"]
    assert a.max_reentries == 0
    assert a.reentry_prompt == ""


def test_reentry_fields_parse_with_max_turns():
    a = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "max_turns": 10,
         "max_reentries": 2, "reentry_prompt": "verify, then deliver"},
    ]}).nodes["a"]
    assert a.max_reentries == 2
    assert a.reentry_prompt == "verify, then deliver"


def test_max_reentries_requires_max_turns():
    with pytest.raises(WorkflowError, match="max_reentries requires max_turns"):
        parse_workflow({"name": "w", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "max_reentries": 1}]})


def test_reentry_prompt_requires_max_reentries():
    with pytest.raises(WorkflowError, match="reentry_prompt requires max_reentries"):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "max_turns": 10, "reentry_prompt": "go on"}]})


def test_max_reentries_negative_raises():
    with pytest.raises(WorkflowError, match="max_reentries must be an int >= 0"):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "max_turns": 10, "max_reentries": -1}]})


def test_max_reentries_bool_rejected():
    # bool is a subclass of int; True == 1 but must still be rejected.
    with pytest.raises(WorkflowError, match="max_reentries must be an int >= 0"):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "max_turns": 10, "max_reentries": True}]})


def test_reentry_prompt_non_string_raises():
    with pytest.raises(WorkflowError, match="reentry_prompt must be a string"):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "max_turns": 10, "max_reentries": 1,
             "reentry_prompt": 7}]})



def test_routing_node_cannot_use_shared_context_binary():
    with pytest.raises(WorkflowError, match="routing node.*cannot use context=.shared."):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "context": "shared",
             "on_pass": "b", "on_fail": "a"},
            {"id": "b", "kind": "work", "next": None}]})


def test_routing_node_cannot_use_shared_context_cases():
    with pytest.raises(WorkflowError, match="routing node.*cannot use context=.shared."):
        parse_workflow({"name": "w", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "context": "shared",
             "cases": {"x": "b", "default": None}},
            {"id": "b", "kind": "work", "next": None}]})


def test_non_routing_shared_node_still_parses():
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "context": "shared", "next": "b"},
        {"id": "b", "kind": "work", "next": None}]})
    assert wf.nodes["a"].context == "shared"


def test_session_field_parses_and_defaults_to_fresh():
    wf = parse_workflow({
        "name": "w", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "session": "persistent", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })
    assert wf.nodes["a"].session == "persistent"
    assert wf.nodes["b"].session == "fresh"


def test_session_rejects_unknown_value():
    with pytest.raises(WorkflowError, match="session"):
        parse_workflow({"name": "w", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "session": "sticky", "next": None}]})


def test_persistent_session_excludes_shared_context():
    with pytest.raises(WorkflowError, match="persistent"):
        parse_workflow({"name": "w", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "session": "persistent",
                                   "context": "shared", "next": None}]})


def test_persistent_session_rejected_on_parallel_units():
    with pytest.raises(WorkflowError, match="persistent"):
        parse_workflow({
            "name": "w", "start": "p",
            "nodes": [
                {"id": "p", "kind": "parallel", "branches": ["b1"], "next": None},
                {"id": "b1", "kind": "work", "session": "persistent"},
            ],
        })


# ---------------------------------------------------------------------------
# output.artifacts — the declared file contract (B2)
# ---------------------------------------------------------------------------

def _artifacts_wf(output):
    return {"name": "t", "start": "s", "output": output,
            "nodes": [{"id": "s", "prompt": "p", "next": None}]}


def test_output_artifacts_parse():
    wf = parse_workflow(_artifacts_wf({"file": True, "artifacts": [
        {"path": "context.json", "description": "Consolidated ticket context"},
        {"path": "evidence.json"},
    ]}))
    assert [a["path"] for a in wf.output["artifacts"]] == ["context.json", "evidence.json"]


def test_output_artifacts_rejects_non_list():
    with pytest.raises(WorkflowError, match="artifacts"):
        parse_workflow(_artifacts_wf({"artifacts": {"path": "x.json"}}))


def test_output_artifacts_rejects_missing_path():
    with pytest.raises(WorkflowError, match="path"):
        parse_workflow(_artifacts_wf({"artifacts": [{"description": "no path"}]}))


def test_output_artifacts_rejects_escaping_path():
    with pytest.raises(WorkflowError, match="relative"):
        parse_workflow(_artifacts_wf({"artifacts": [{"path": "../evil.json"}]}))
    with pytest.raises(WorkflowError, match="relative"):
        parse_workflow(_artifacts_wf({"artifacts": [{"path": "/abs/evil.json"}]}))


def test_output_artifacts_rejects_duplicate_paths():
    with pytest.raises(WorkflowError, match="duplicate"):
        parse_workflow(_artifacts_wf({"artifacts": [
            {"path": "context.json"}, {"path": "context.json"},
        ]}))


# ---------------------------------------------------------------------------
# node_label / node_description precedence
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from durin.workflow.spec import node_description, node_label


def test_node_label_prefers_the_id_over_the_prompt():
    node = SimpleNamespace(id="draft-note", title="", prompt="You are the note drafter. Write it.",
                           command="", script="")
    assert node_label(node) == "Draft note"


def test_node_label_still_prefers_an_author_title():
    node = SimpleNamespace(id="draft-note", title="Draft the note", prompt="You are the drafter.",
                           command="", script="")
    assert node_label(node) == "Draft the note"


def test_node_label_of_a_script_node_is_its_command():
    node = SimpleNamespace(id="structure-check", title="", prompt="", command="",
                           script="check-note-structure.sh")
    assert node_label(node) == "check-note-structure.sh"


def test_node_description_carries_the_prompt_sentence():
    node = SimpleNamespace(id="judge", title="", prompt="You are the JUDGE. Be strict.",
                           command="", script="")
    assert node_description(node) == "You are the JUDGE"
