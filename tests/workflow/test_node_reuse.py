"""reuse="if-unchanged": a node skips its runner when its output_file's recorded
producer (node definition + resolved model/provider/params) matches the runner's
CURRENT producer identity exactly. Mirrors the engine-fixture pattern of
test_engine_artifacts.py: a plain node_runner double, WorkflowEngine driven
directly, work_dir_override to make two separate .run() calls share one working
folder (simulating a workflow re-run against the same work directory)."""

import pytest

from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.spec import WorkflowError, parse_workflow

_UNSET = object()


class _Runner:
    """A node_runner test double whose reuse_identity() reports exactly what a
    live dispatch would stamp into provenance — the invariant AgentNodeRunner
    keeps in real code — unless identity_override is set, to simulate the
    runner's CURRENT producer having drifted since the artifact was recorded."""

    def __init__(self, output="out", model="m1", provider="p1", params_hash="h1"):
        self.calls: list[str] = []
        self.upstream_seen: dict[str, str | None] = {}
        self.output = output
        self.model = model
        self.provider = provider
        self.params_hash = params_hash
        self.identity_override = _UNSET

    def __call__(self, req):
        self.calls.append(req.node.id)
        self.upstream_seen[req.node.id] = req.upstream_output
        return NodeRunResponse(output=self.output, model=self.model,
                               provider=self.provider, params_hash=self.params_hash)

    def reuse_identity(self, node):
        if self.identity_override is not _UNSET:
            return self.identity_override
        return {"model": self.model, "provider": self.provider, "params_hash": self.params_hash}


def _plan_node(**overrides):
    node = {"id": "plan", "kind": "work",
            "output_schema": {"type": "object"}, "output_file": "out.json", "next": None}
    node.update(overrides)
    return node


def _wf(node, *extra_nodes):
    return parse_workflow({"name": "w", "start": "plan", "nodes": [node, *extra_nodes]})


def test_reuse_skips_when_producer_identical(tmp_path):
    runner = _Runner(output='{"x": 1}')
    engine = WorkflowEngine(runner, workspace=str(tmp_path))
    first = engine.run(_wf(_plan_node()), "t")
    assert first.status == "completed"
    assert runner.calls == ["plan"]

    second = engine.run(_wf(_plan_node(reuse="if-unchanged")), "t",
                        work_dir_override=first.output_dir)

    assert second.status == "completed"
    assert runner.calls == ["plan"]                     # never dispatched a 2nd time
    assert second.runs[0].status == "reused"
    assert second.runs[0].origin_run_id == first.run_id
    assert second.final_output == '{"x": 1}'            # == the artifact's content


def test_reuse_reruns_when_node_spec_changed(tmp_path):
    runner = _Runner(output='{"x": 1}')
    engine = WorkflowEngine(runner, workspace=str(tmp_path))
    first = engine.run(_wf(_plan_node()), "t")
    assert runner.calls == ["plan"]

    changed = _plan_node(reuse="if-unchanged", prompt="a different prompt now")
    second = engine.run(_wf(changed), "t", work_dir_override=first.output_dir)

    assert second.status == "completed"
    assert runner.calls == ["plan", "plan"]             # dispatched again
    assert second.runs[0].status != "reused"


def test_reuse_reruns_when_model_changed(tmp_path):
    runner = _Runner(output='{"x": 1}')
    engine = WorkflowEngine(runner, workspace=str(tmp_path))
    first = engine.run(_wf(_plan_node()), "t")
    assert runner.calls == ["plan"]

    runner.identity_override = {"model": "m2", "provider": "p1", "params_hash": "h1"}
    second = engine.run(_wf(_plan_node(reuse="if-unchanged")), "t",
                        work_dir_override=first.output_dir)

    assert second.status == "completed"
    assert runner.calls == ["plan", "plan"]             # dispatched again
    assert second.runs[0].status != "reused"


def test_reuse_reruns_without_provenance(tmp_path):
    # A "legacy" work folder: the artifact exists but no .provenance.json was ever
    # written for it (e.g. from before this feature, or a differently-run pass).
    legacy_dir = tmp_path / "legacy-work"
    legacy_dir.mkdir()
    (legacy_dir / "out.json").write_text('{"x": "stale"}', encoding="utf-8")

    runner = _Runner(output='{"x": "fresh"}')
    engine = WorkflowEngine(runner, workspace=str(tmp_path))
    result = engine.run(_wf(_plan_node(reuse="if-unchanged")), "t",
                        work_dir_override=str(legacy_dir))

    assert result.status == "completed"
    assert runner.calls == ["plan"]                     # dispatched — nothing to trust
    assert result.runs[0].status != "reused"
    assert (legacy_dir / "out.json").read_text() == '{"x": "fresh"}'


def test_reused_node_output_feeds_next_node(tmp_path):
    next_node = {"id": "next", "kind": "work", "next": None}
    runner = _Runner(output='{"x": 1}')
    engine = WorkflowEngine(runner, workspace=str(tmp_path))
    first = engine.run(_wf(_plan_node(next="next"), next_node), "t")
    assert first.status == "completed"

    runner.calls.clear()
    second = engine.run(_wf(_plan_node(reuse="if-unchanged", next="next"), next_node), "t",
                        work_dir_override=first.output_dir)

    assert second.status == "completed"
    assert runner.calls == ["next"]                     # plan was reused, not dispatched
    assert runner.upstream_seen["next"] == '{"x": 1}'   # next saw the reused file content


def test_reuse_without_output_file_fails_validation():
    with pytest.raises(WorkflowError, match="reuse"):
        parse_workflow({
            "name": "w", "start": "plan",
            "nodes": [{"id": "plan", "kind": "work", "reuse": "if-unchanged", "next": None}],
        })


def test_reuse_invalid_value_fails_validation():
    with pytest.raises(WorkflowError, match="reuse"):
        parse_workflow({"name": "w", "start": "plan", "nodes": [_plan_node(reuse="always")]})
