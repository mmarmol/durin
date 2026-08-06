"""The in-flight node must be openable, not merely nameable.

``active_node`` names the node executing right now. Without its iteration and
session key a reader cannot open the node's conversation: the key is not
derivable from the node id alone, so a surface would have to guess — and would
guess wrong for a persistent-session node (no iteration suffix) or a fan-out
worker (an extra worker suffix).
"""

from durin.workflow import run_log
from durin.workflow.engine import NodeRunRequest, NodeRunResponse, WorkflowEngine
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.session_keys import node_session_key
from durin.workflow.spec import parse_workflow


def _request(node, *, run_id="r1", iteration=1, worker_index=None, workspace_override=None):
    return NodeRunRequest(
        node=node, task="t", upstream_output=None, shared_context=[],
        run_id=run_id, iteration=iteration, root_session_key=None,
        worker_index=worker_index, workspace_override=workspace_override,
    )


def _workflow(**node_extra):
    return parse_workflow({
        "name": "obs", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "next": None, **node_extra}],
    })


def test_helper_agrees_with_the_runner_on_a_plain_node():
    node = _workflow().nodes["a"]
    req = _request(node, iteration=2)
    assert node_session_key("r1", node, 2) == AgentNodeRunner._session_key(req)
    assert node_session_key("r1", node, 2) == "workflow:r1:a:2"


def test_helper_agrees_with_the_runner_on_a_persistent_node():
    node = _workflow(session="persistent").nodes["a"]
    req = _request(node, iteration=3)
    assert node_session_key("r1", node, 3) == AgentNodeRunner._session_key(req)
    assert node_session_key("r1", node, 3) == "workflow:r1:a"


def test_helper_agrees_with_the_runner_on_a_fanout_worker():
    node = _workflow(session="persistent").nodes["a"]
    req = _request(node, iteration=1, worker_index=2)
    key = node_session_key("r1", node, 1, worker_index=2)
    assert key == AgentNodeRunner._session_key(req)
    # A worker is per-unit even when the node declares persistence.
    assert key == "workflow:r1:a:1:2"


def test_active_node_carries_iteration_and_session_key_while_running(tmp_path):
    seen = {}

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        manifest = run_log.read_manifest(tmp_path, "obs", "r1") or {}
        seen["active"] = manifest.get("active_node")
        return NodeRunResponse(output="done", session_key=AgentNodeRunner._session_key(req))

    engine = WorkflowEngine(node_runner=runner, workspace=str(tmp_path),
                            run_id_factory=lambda: "r1")
    engine.run(_workflow(), "task")

    assert seen["active"]["node_id"] == "a"
    assert seen["active"]["iteration"] == 1
    assert seen["active"]["session_key"] == "workflow:r1:a:1"
    # Cleared once the node finishes: a completed node pinned as running would
    # render as a spinner whose clock never stops.
    final = run_log.read_manifest(tmp_path, "obs", "r1") or {}
    assert final.get("active_node") is None


def test_active_node_of_a_script_node_advertises_no_session(tmp_path):
    seen = {}
    workflow = parse_workflow({
        "name": "obs", "start": "s",
        "nodes": [{"id": "s", "kind": "script", "command": "true", "next": None}],
    })

    def script_runner(req: NodeRunRequest) -> NodeRunResponse:
        manifest = run_log.read_manifest(tmp_path, "obs", "r1") or {}
        seen["active"] = manifest.get("active_node")
        return NodeRunResponse(output="", session_key=None, exit_code=0)

    engine = WorkflowEngine(node_runner=lambda req: NodeRunResponse(output=""),
                            script_runner=script_runner, workspace=str(tmp_path),
                            run_id_factory=lambda: "r1")
    engine.run(workflow, "task")

    # A script node persists no conversation; advertising a key would point a
    # reader at a session that does not exist.
    assert seen["active"]["session_key"] is None
    assert seen["active"]["iteration"] == 1
