"""A resumed step with shared context sees what it would have seen."""
import json
import sys
from pathlib import Path

import pytest
from loguru import logger

from durin.workflow.engine import (
    NodeExecutionError,
    NodeRunResponse,
    WorkflowEngine,
    build_resume_state,
)
from durin.workflow.spec import parse_workflow

SESSIONS: dict[str, list[dict]] = {}


@pytest.fixture(autouse=True)
def _own_sessions(monkeypatch):
    """Each test saves into a store of its own, so no test can pass on the
    sessions another test left behind."""
    monkeypatch.setattr(sys.modules[__name__], "SESSIONS", {})


@pytest.fixture
def engine_warnings():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING", format="{message}",
                      filter="durin.workflow.engine")
    yield lines
    logger.remove(sink)


class _Runner:
    """Saves each step's conversation the way the real runner does: the system
    prompt, the shared conversation it was given, then its own turns."""

    def __init__(self, fail=None):
        self.fail, self.seen = dict(fail or {}), []

    def __call__(self, req):
        self.seen.append((req.node.id, [m["content"] for m in req.shared_context]))
        if self.fail.get(req.node.id, 0) > 0:
            self.fail[req.node.id] -= 1
            raise NodeExecutionError(req.node.id, req.iteration, None, RuntimeError("transient"))
        own = [{"role": "user", "content": f"to {req.node.id}"},
               {"role": "assistant", "content": f"{req.node.id} said"}]
        key = f"workflow:r1:{req.node.id}:{req.iteration}"
        SESSIONS[key] = [{"role": "system", "content": "sys"}, *req.shared_context, *own]
        return NodeRunResponse(output=f"{req.node.id}-out", session_key=key, messages=own)


def _loader(key):
    return [m for m in SESSIONS[key] if m["role"] != "system"]


def _engine(tmp_path, runner, loader=_loader):
    return WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r1",
                          workspace=str(tmp_path), shared_context_loader=loader)


def _manifest(tmp_path):
    return json.loads((Path(tmp_path) / "workflows-runs" / "w" / "r1.json").read_text())


def _chain():
    return parse_workflow({"name": "w", "start": "s1", "nodes": [
        {"id": "s1", "kind": "work", "context": "shared", "next": "s2"},
        {"id": "s2", "kind": "work", "context": "shared", "next": "s3"},
        {"id": "s3", "kind": "work", "context": "shared", "next": None}]})


def _resume(tmp_path, runner, **kw):
    return _engine(tmp_path, runner, **kw).run(
        _chain(), "t", resume=build_resume_state(_manifest(tmp_path), ""))


def test_a_retried_shared_step_sees_the_conversation_it_saw_the_first_time(tmp_path):
    runner = _Runner(fail={"s3": 1})
    assert _engine(tmp_path, runner).run(_chain(), "t").status == "aborted"
    assert _manifest(tmp_path)["shared_session"] == "workflow:r1:s2:1"
    assert _resume(tmp_path, runner).status == "completed"
    first, retry = [seen for node, seen in runner.seen if node == "s3"]
    assert first == ["to s1", "s1 said", "to s2", "s2 said"] and retry == first


def test_the_pointer_survives_a_walk_that_finishes_no_shared_step(tmp_path):
    runner = _Runner(fail={"s2": 1, "s3": 2})
    _engine(tmp_path, runner).run(_chain(), "t")            # s1 ok, s2 fails
    assert _manifest(tmp_path)["shared_session"] == "workflow:r1:s1:1"
    _resume(tmp_path, runner)                               # s2 ok, s3 fails
    assert _manifest(tmp_path)["shared_session"] == "workflow:r1:s2:2"
    _resume(tmp_path, runner)                               # s3 fails again: nothing finishes
    assert _manifest(tmp_path)["shared_session"] == "workflow:r1:s2:2"
    assert _resume(tmp_path, runner).status == "completed"
    assert _manifest(tmp_path)["shared_session"] == "workflow:r1:s3:3"
    assert [seen for node, seen in runner.seen if node == "s3"] == [
        ["to s1", "s1 said", "to s2", "s2 said"]] * 3


def test_a_missing_shared_session_does_not_stop_the_resume(tmp_path):
    runner = _Runner(fail={"s3": 1})
    _engine(tmp_path, runner).run(_chain(), "t")

    def gone(key):
        raise KeyError(key)

    assert _resume(tmp_path, runner, loader=gone).status == "completed"
    assert [seen for node, seen in runner.seen if node == "s3"][-1] == []


def test_an_empty_shared_session_resumes_and_says_the_conversation_is_gone(
        tmp_path, engine_warnings):
    runner = _Runner(fail={"s3": 1})
    _engine(tmp_path, runner).run(_chain(), "t")
    assert _resume(tmp_path, runner, loader=lambda key: []).status == "completed"
    assert [seen for node, seen in runner.seen if node == "s3"][-1] == []
    assert len(engine_warnings) == 1 and "workflow:r1:s2:1" in engine_warnings[0]
    assert "r1" in engine_warnings[0] and "starts empty" in engine_warnings[0]


def test_a_session_never_saved_resumes_through_the_real_reader_and_says_so(
        tmp_path, engine_warnings):
    from durin.session.manager import SessionManager
    from durin.workflow.node_runner import shared_context_from_session
    sessions = SessionManager(tmp_path)
    runner = _Runner(fail={"s3": 1})
    _engine(tmp_path, runner).run(_chain(), "t")
    assert _resume(tmp_path, runner, loader=lambda key: shared_context_from_session(
        sessions, key)).status == "completed"
    assert [seen for node, seen in runner.seen if node == "s3"][-1] == []
    assert len(engine_warnings) == 1 and "workflow:r1:s2:1" in engine_warnings[0]
    assert "starts empty" in engine_warnings[0]


def test_the_loader_returns_a_saved_session_without_its_system_prompt(tmp_path):
    from durin.session.manager import Session, SessionManager
    from durin.workflow.node_runner import shared_context_from_session
    sessions = SessionManager(tmp_path)
    sessions.save(Session(key="workflow:r1:s2:1", messages=[
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "to s1"}, {"role": "assistant", "content": "s1 said"}]))
    assert [m["content"] for m in shared_context_from_session(sessions, "workflow:r1:s2:1")] == [
        "to s1", "s1 said"]
    assert shared_context_from_session(sessions, "workflow:r1:none:1") == []
