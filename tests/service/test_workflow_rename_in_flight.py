"""A workflow with a run in flight cannot be renamed: the rename would move and
rewrite the record its walk is still writing, under a name the walk does not
know. A run whose process is provably gone does not hold the rename."""
import json
import threading
from pathlib import Path

import pytest

from durin.automations.spec import parse_automation
from durin.automations.store import save_automation
from durin.service.principal import Principal
from durin.service.types import ValidationFailedError
from durin.service.workflows import WorkflowRenameCommand, WorkflowSaveCommand, WorkflowsService
from durin.utils import process_tree
from durin.utils.process_tree import process_identity
from durin.workflow import run_log

_VALID = {"name": "wf", "start": "a", "nodes": [{"id": "a", "kind": "work"}]}
_CALLER = {"name": "caller", "start": "call",
           "nodes": [{"id": "call", "kind": "subworkflow", "workflow": "wf", "next": None}]}
# A pid far above any real pid_max, so the owner reads as long gone.
_DEAD = {"pid": 2**22 + 4242, "started": "never"}
_REFUSAL = "workflow 'wf' has a run in progress; it can be renamed once that run has ended."


async def _setup(tmp_path) -> WorkflowsService:
    svc = WorkflowsService(workspace=tmp_path)
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), Principal.local())
    await svc.save(WorkflowSaveCommand(name="caller", definition=_CALLER), Principal.local())
    save_automation(tmp_path, parse_automation({"name": "nightly", "workflow": "wf"}))
    return svc


def _record(tmp_path, run_id: str, **fields) -> None:
    run_log.start_run(tmp_path, "wf", run_id, root_session_key="s", started_at=1.0,
                      position={"node": "a", "seq": 1})
    path = run_log._record_path(tmp_path, "wf", run_id)
    rec = {**json.loads(path.read_text(encoding="utf-8")), **fields}
    run_log._write_json(path, rec)


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


async def _refused(svc, tmp_path) -> None:
    before = _tree(tmp_path)
    with pytest.raises(ValidationFailedError) as refused:
        await svc.rename(WorkflowRenameCommand(name="wf", target="wf-new"), Principal.local())
    assert str(refused.value) == _REFUSAL
    # Nothing moved or rewritten: the definition, its runs, the caller that
    # names it, the automation that runs it, the version history.
    assert _tree(tmp_path) == before


@pytest.mark.asyncio
async def test_a_run_whose_owner_is_alive_holds_the_rename(tmp_path):
    svc = await _setup(tmp_path)
    _record(tmp_path, "done", status="completed", owner=None)
    _record(tmp_path, "live", owner=process_identity())
    await _refused(svc, tmp_path)


@pytest.mark.asyncio
async def test_a_run_whose_owner_cannot_be_observed_holds_the_rename(tmp_path, monkeypatch):
    svc = await _setup(tmp_path)
    _record(tmp_path, "live", owner=_DEAD)

    def cannot_observe(*_a, **_kw):
        raise process_tree.ObservationError("ps timed out")

    monkeypatch.setattr(process_tree, "_observe", cannot_observe)
    await _refused(svc, tmp_path)


@pytest.mark.asyncio
async def test_the_check_runs_off_the_event_loop(tmp_path, monkeypatch):
    svc = await _setup(tmp_path)
    _record(tmp_path, "live", owner=process_identity())
    real = run_log.run_in_flight
    threads = []

    def recording(*a, **kw):
        threads.append(threading.current_thread())
        return real(*a, **kw)

    monkeypatch.setattr(run_log, "run_in_flight", recording)
    await _refused(svc, tmp_path)
    assert threads and threads[0] is not threading.main_thread()


@pytest.mark.parametrize("fields", [
    {"status": "completed", "owner": None},
    {"status": "needs_input", "needs_input_node": "a", "owner": None},
    {"status": "aborted", "failed_node": "a", "owner": None},
    {"status": "crashed", "owner": None},
    {"owner": _DEAD},                       # running, its process provably gone
])
@pytest.mark.asyncio
async def test_a_run_not_in_flight_does_not_hold_the_rename(tmp_path, fields):
    svc = await _setup(tmp_path)
    _record(tmp_path, "r1", **fields)
    res = await svc.rename(WorkflowRenameCommand(name="wf", target="wf-new"), Principal.local())
    assert res.name == "wf-new"
    moved = run_log.read_manifest(tmp_path, "wf-new", "r1")
    assert moved is not None and moved["workflow"] == "wf-new"
    assert not (run_log.runs_root(tmp_path) / "wf").exists()
