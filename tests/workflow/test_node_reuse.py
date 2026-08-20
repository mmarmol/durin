"""reuse="if-unchanged": a node skips its runner when its output_file's recorded
producer (node definition + resolved model/provider/params) matches the runner's
CURRENT producer identity exactly. Mirrors the engine-fixture pattern of
test_engine_artifacts.py: a plain node_runner double, WorkflowEngine driven
directly, work_dir_override to make two separate .run() calls share one working
folder (simulating a workflow re-run against the same work directory)."""

from pathlib import Path

import pytest

from durin.workflow import provenance, run_log
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

    # The DURABLE record, not just the in-memory result: origin_run_id must
    # survive into the manifest run_log.py writes, or no reader (webui, API,
    # a later dream pass) can ever tell which run actually produced the file.
    manifest = run_log.read_manifest(tmp_path, "w", second.run_id)
    assert manifest["runs"][0]["origin_run_id"] == first.run_id
    assert manifest["runs"][0]["status"] == "reused"


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


# ---------------------------------------------------------------------------
# C2/I1: the gate must also close on same-run revisits and artifact/input drift
# ---------------------------------------------------------------------------


def test_reuse_never_fires_on_a_same_run_revisit_after_a_loop_back(tmp_path):
    """producer(reuse) -> judge(on_fail loops back to producer). The judge FAILs
    once then PASSes: visit 2 of 'plan' happens in the SAME run, with the SAME
    producer identity as visit 1 — it must still RUN, never reuse."""
    calls: list[str] = []
    judge_calls = {"n": 0}

    def runner(req):
        calls.append(req.node.id)
        if req.node.id == "judge":
            judge_calls["n"] += 1
            return NodeRunResponse(output="PASS" if judge_calls["n"] > 1 else "FAIL")
        return NodeRunResponse(output='{"x": 1}', model="m1", provider="p1", params_hash="h1")

    runner.reuse_identity = lambda node: {"model": "m1", "provider": "p1", "params_hash": "h1"}

    wf = parse_workflow({
        "name": "w", "start": "plan",
        "nodes": [
            _plan_node(reuse="if-unchanged", next="judge"),
            {"id": "judge", "kind": "work", "mode": "explore", "on_pass": None, "on_fail": "plan"},
        ],
    })
    result = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")

    assert result.status == "completed"
    assert calls.count("plan") == 2                     # dispatched on BOTH visits
    plan_runs = [r for r in result.runs if r.node_id == "plan"]
    assert [r.status for r in plan_runs] == ["ok", "ok"]  # neither visit reused


def test_reuse_hit_never_fires_on_same_run_revisit_even_with_identical_input(tmp_path):
    """Focused unit test isolating the iteration<=1 rule from input_hash: stamp an
    entry, then call _reuse_hit directly with iteration=2 and the EXACT same
    (task, node_input) that produced it — every other signal agrees, and it must
    still return None. Proves the belt-and-suspenders rule is enforced on its own,
    not merely as a side effect of upstream text normally changing on a loop-back."""

    class _Runner:
        def __call__(self, req):
            raise AssertionError("not dispatched by this test")

        def reuse_identity(self, node):
            return {"model": "m1", "provider": "p1", "params_hash": "h1"}

    runner = _Runner()
    engine = WorkflowEngine(runner, workspace=str(tmp_path))
    node = parse_workflow({
        "name": "w", "start": "plan", "nodes": [_plan_node()],
    }).nodes["plan"]

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "out.json").write_text('{"x": 1}', encoding="utf-8")
    provenance.record(work_dir, "out.json", {
        "run_id": "origin", "node_hash": provenance.reuse_hash(node.raw),
        "model": "m1", "provider": "p1", "params_hash": "h1",
        "input_hash": provenance.input_hash("t", "up"),
        "content_sha256": provenance.content_sha256('{"x": 1}'),
    })

    # iteration=1 with the identical inputs is a genuine hit...
    hit = engine._reuse_hit(node, str(work_dir), task="t", node_input="up", iteration=1)
    assert hit is not None
    # ...but iteration=2 (a same-run revisit) never is, everything else equal.
    assert engine._reuse_hit(node, str(work_dir), task="t", node_input="up", iteration=2) is None


def test_reuse_reruns_when_artifact_mutated_after_stamping(tmp_path):
    runner = _Runner(output='{"x": 1}')
    engine = WorkflowEngine(runner, workspace=str(tmp_path))
    first = engine.run(_wf(_plan_node()), "t")
    assert runner.calls == ["plan"]

    # The file changes on disk AFTER it was stamped (external edit, a restored
    # backup, anything) — the recorded content_sha256 no longer matches.
    (Path(first.output_dir) / "out.json").write_text('{"x": "tampered"}', encoding="utf-8")

    second = engine.run(_wf(_plan_node(reuse="if-unchanged")), "t",
                        work_dir_override=first.output_dir)

    assert second.status == "completed"
    assert runner.calls == ["plan", "plan"]              # dispatched again
    assert second.runs[0].status != "reused"


def test_reuse_reruns_when_upstream_output_changed(tmp_path):
    """producer -> consumer(reuse). The SAME producer node emits different text on
    the second run — consumer's composed input therefore differs, so its stamped
    input_hash no longer matches even though consumer's own definition, model,
    provider and params never changed."""
    producer = {"id": "producer", "kind": "work", "next": "consumer"}
    consumer = {"id": "consumer", "kind": "work", "output_schema": {"type": "object"},
                "output_file": "out.json", "next": None}

    runner = _Runner(output="v1")
    engine = WorkflowEngine(runner, workspace=str(tmp_path))
    first = engine.run(parse_workflow({"name": "w", "start": "producer", "nodes": [producer, consumer]}), "t")
    assert first.status == "completed"
    assert runner.calls == ["producer", "consumer"]

    runner.calls.clear()
    runner.output = "v2"     # the producer's own output drifted between runs
    consumer_reused = {**consumer, "reuse": "if-unchanged"}
    second = engine.run(
        parse_workflow({"name": "w", "start": "producer", "nodes": [producer, consumer_reused]}),
        "t", work_dir_override=first.output_dir)

    assert second.status == "completed"
    assert runner.calls == ["producer", "consumer"]      # consumer dispatched again
    assert second.runs[1].status != "reused"


def test_reuse_record_failure_drops_the_stale_entry(tmp_path, monkeypatch):
    runner = _Runner(output='{"x": 1}')
    engine = WorkflowEngine(runner, workspace=str(tmp_path))
    first = engine.run(_wf(_plan_node()), "t")
    assert first.status == "completed"
    work_dir = Path(first.output_dir)
    assert provenance.load(work_dir).get("out.json") is not None   # stamped by the first run

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(provenance, "record", _boom)

    second = engine.run(_wf(_plan_node()), "t", work_dir_override=first.output_dir)

    assert second.status == "completed"          # a provenance failure never breaks the node
    # The stale entry from the FIRST run must not be left standing over the
    # second run's (unstamped) content.
    assert provenance.load(work_dir).get("out.json") is None
