"""Advisory warnings on workflow save: mode typos and allowlist entries that
can never apply to a work node."""

from durin.agent.agent_mode import AgentMode, register_mode
from durin.workflow.editing import definition_warnings, save_workflow_definition
from durin.workflow.spec import parse_workflow


def _wf(nodes):
    return parse_workflow({"name": "t", "start": nodes[0]["id"], "nodes": nodes})


def test_unknown_mode_warns_about_silent_build_fallback():
    wf = _wf([{"id": "a", "mode": "no-such-mode", "prompt": "x", "next": None}])
    warnings = definition_warnings(wf)
    assert len(warnings) == 1
    assert "no-such-mode" in warnings[0]
    assert "build" in warnings[0]


def test_known_read_mode_is_clean():
    wf = _wf([
        {"id": "a", "mode": "build", "tools": "default", "prompt": "x", "next": "b"},
        {"id": "b", "mode": "read", "tools": "default", "prompt": "y", "next": None},
    ])
    assert definition_warnings(wf) == []


def test_custom_mode_with_main_only_tools_warns():
    register_mode(AgentMode(
        name="tmp-warn-test",
        description="test",
        allowed=frozenset({"read_file", "spawn", "cron"}),
    ))
    try:
        wf = _wf([{"id": "a", "mode": "tmp-warn-test", "prompt": "x", "next": None}])
        warnings = definition_warnings(wf)
        assert len(warnings) == 1
        assert "spawn" in warnings[0] and "cron" in warnings[0]
        assert "read_file" not in warnings[0]
    finally:
        from durin.agent import agent_mode
        agent_mode._REGISTRY.pop("tmp-warn-test", None)


def test_mcp_allowlist_entries_are_not_flagged():
    register_mode(AgentMode(
        name="tmp-mcp-test",
        description="test",
        allowed=frozenset({"read_file", "mcp_github_search"}),
    ))
    try:
        wf = _wf([{"id": "a", "mode": "tmp-mcp-test", "prompt": "x", "next": None}])
        assert definition_warnings(wf) == []
    finally:
        from durin.agent import agent_mode
        agent_mode._REGISTRY.pop("tmp-mcp-test", None)


def test_save_returns_warnings(tmp_path):
    result = save_workflow_definition(
        tmp_path,
        "warned",
        {"start": "a", "nodes": [{"id": "a", "mode": "typo-mode", "prompt": "x", "next": None}]},
        reason="test",
        actor="test",
        must_exist=False,
    )
    assert result["ok"] is True
    assert any("typo-mode" in w for w in result["warnings"])


def test_save_omits_warnings_when_clean(tmp_path):
    result = save_workflow_definition(
        tmp_path,
        "clean",
        {"start": "a", "nodes": [{"id": "a", "mode": "build", "prompt": "x", "next": None}]},
        reason="test",
        actor="test",
        must_exist=False,
    )
    assert result["ok"] is True
    assert "warnings" not in result
