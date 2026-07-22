"""Global per-kind parallel concurrency caps.

Script branches are cheap (API calls, Athena queries) and can run wider than
LLM branches, whose width is bounded by provider rate limits. The caps are
GLOBAL config (`workflow.parallel_llm_concurrency` = 2,
`workflow.parallel_script_concurrency` = 4), not per-workflow: a parallel node
without an explicit ``max_concurrency`` gets each branch kind bounded by its
own cap — script branches never queue behind LLM branches. An explicit
``max_concurrency`` keeps the old uniform behavior (back-compat for existing
definitions).
"""

import threading
import time
from pathlib import Path

from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.script_runner import ScriptNodeRunner
from durin.workflow.spec import parse_workflow


def _write_script(workspace: Path, name: str) -> None:
    d = workspace / "workflows" / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("import time\ntime.sleep(0.15)\nprint('ok')\n", encoding="utf-8")
    p.chmod(0o755)


def _wf(n_scripts: int, n_llm: int, **fan_extra):
    nodes = [{"id": "fan", "kind": "parallel",
              "branches": [f"s{i}" for i in range(n_scripts)] + [f"w{i}" for i in range(n_llm)],
              "reconcile": "read", "next": "join", **fan_extra}]
    nodes += [{"id": f"s{i}", "kind": "script", "script": "slow.py"} for i in range(n_scripts)]
    nodes += [{"id": f"w{i}", "kind": "work"} for i in range(n_llm)]
    nodes += [{"id": "join", "kind": "work", "next": None}]
    return parse_workflow({"name": "d", "start": "fan", "nodes": nodes})


class _Tracker:
    """Counts concurrently-active work-node runs (the LLM lane)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def run(self, req):
        if req.node.id.startswith("w"):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.15)
            with self.lock:
                self.active -= 1
        return NodeRunResponse(output=f"{req.node.id}-out", session_key=None, messages=[])


def _engine(tmp_path, node_runner, *, llm_cap=2, script_cap=4):
    return WorkflowEngine(
        node_runner=node_runner,
        script_runner=ScriptNodeRunner(tmp_path),
        run_id_factory=lambda: "r1",
        workspace=str(tmp_path),
        parallel_llm_concurrency=llm_cap,
        parallel_script_concurrency=script_cap,
    )


class TestSpec:
    def test_max_concurrency_defaults_to_none(self):
        wf = _wf(1, 1)
        assert wf.nodes["fan"].max_concurrency is None

    def test_explicit_max_concurrency_is_kept(self):
        wf = _wf(1, 1, max_concurrency=3)
        assert wf.nodes["fan"].max_concurrency == 3

    def test_invalid_max_concurrency_is_rejected(self):
        import pytest

        from durin.workflow.spec import WorkflowError

        with pytest.raises(WorkflowError):
            _wf(1, 1, max_concurrency=0)
        with pytest.raises(WorkflowError):
            _wf(1, 1, max_concurrency=True)


class TestSplitCaps:
    def test_script_branches_run_wider_than_the_llm_cap(self, tmp_path):
        # 3 scripts + 2 work branches, llm_cap=1, script_cap=3: with the old
        # uniform pool of 1 everything would serialize; split caps let all 3
        # scripts overlap while work branches go one at a time.
        _write_script(tmp_path, "slow.py")
        tracker = _Tracker()
        t0 = time.monotonic()
        res = _engine(tmp_path, tracker.run, llm_cap=1, script_cap=3).run(
            _wf(3, 2), "t")
        wall = time.monotonic() - t0

        assert res.status == "completed"
        assert tracker.peak == 1                      # LLM lane honored its cap
        # 3 overlapped scripts (~0.15s) + 2 serial work runs (~0.3s) comfortably
        # beat the ~0.75s a uniform pool of 1 would need for 5 x 0.15s branches.
        assert wall < 0.7, f"scripts appear serialized behind the LLM cap ({wall:.2f}s)"

    def test_explicit_cap_stays_uniform_across_kinds(self, tmp_path):
        # max_concurrency=1 on the node: strictly one branch at a time,
        # regardless of kind — exactly the pre-split behavior.
        _write_script(tmp_path, "slow.py")
        tracker = _Tracker()
        t0 = time.monotonic()
        res = _engine(tmp_path, tracker.run, llm_cap=4, script_cap=4).run(
            _wf(2, 2, max_concurrency=1), "t")
        wall = time.monotonic() - t0

        assert res.status == "completed"
        assert tracker.peak == 1
        assert wall > 0.55, f"4 branches under an explicit cap of 1 finished too fast ({wall:.2f}s)"


class TestWorkerFanOut:
    def test_dynamic_workers_use_the_llm_cap_when_node_cap_is_unset(self, tmp_path):
        wf = parse_workflow({"name": "d", "start": "plan", "nodes": [
            {"id": "plan", "kind": "work", "next": "fan"},
            {"id": "fan", "kind": "parallel", "list_from": "plan",
             "worker": "worker", "next": "join"},
            {"id": "worker", "kind": "work"},
            {"id": "join", "kind": "work", "next": None},
        ]})

        lock = threading.Lock()
        state = {"active": 0, "peak": 0}

        def node_runner(req):
            if req.node.id == "plan":
                return NodeRunResponse(output="- a\n- b\n- c\n- d", session_key=None, messages=[])
            if req.node.id == "worker":
                with lock:
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                time.sleep(0.1)
                with lock:
                    state["active"] -= 1
            return NodeRunResponse(output=f"{req.node.id}-out", session_key=None, messages=[])

        res = _engine(tmp_path, node_runner, llm_cap=2, script_cap=4).run(wf, "t")
        assert res.status == "completed"
        assert state["peak"] == 2      # bounded by the LLM cap, not the script cap


class TestConfigDefaults:
    def test_workflow_config_carries_the_split_caps(self):
        from durin.config.schema import WorkflowConfig

        cfg = WorkflowConfig()
        assert cfg.parallel_llm_concurrency == 2
        assert cfg.parallel_script_concurrency == 4
