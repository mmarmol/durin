"""A workflow run record is replaced whole, never rewritten in place.

Run manifests are read while they are being rewritten: by the runs panel and
the ``tasks`` tool, by the crash sweep, by the folder pruner deciding which
runs are still live, and by the run's own next rewrite, which starts from the
previous manifest. A rewrite that truncates the file and then writes it lets
such a reader see a torn file, which every reader treats as "no record": a
live run looks gone, a rewrite drops the fields it meant to carry over. The
same holds for the improve pass's cursor and pending-validation record and for
the provenance file of a run's working folder.

Each test pauses the writer halfway through the bytes it writes and reads the
record at that moment, from another thread. The reader must see the record as
it was or as it is after the write, never a partial one.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path

import pytest

from durin.workflow import provenance, run_log
from durin.workflow.result import NodeRun, WorkflowResult
from durin.workflow.workflow_improve_dream import _read_pending, _write_pending

DEAD_OWNER = {"pid": 2**22 + 4242, "started": "never"}


def _is_target(file, target: Path) -> bool:
    """Whether an open() call writes ``target``: the file itself by name, or
    the temporary file an atomic write of it goes through (opened by fd)."""
    if isinstance(file, int):
        ino = os.fstat(file).st_ino
        return any(p.stat().st_ino == ino for p in target.parent.glob(f".{target.name}.*"))
    return Path(file).name == target.name


def _read_mid_write(monkeypatch: pytest.MonkeyPatch, target: Path, write, read):
    """Run ``write`` on its own thread, stop it halfway through the bytes of the
    first file it opens to write ``target``, run ``read`` on this thread at that
    moment, then let the write finish. Returns what ``read`` saw.

    Both ways of writing a file go through io.open, a plain write of the target
    and an atomic write's temporary file alike, so the stop lands mid-write
    whichever way the code under test writes."""
    half_written, resume = threading.Event(), threading.Event()
    armed = {"on": True}
    real_open = io.open

    class _StopsHalfway:
        def __init__(self, f):
            self._f = f

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._f.__exit__(*exc)

        def write(self, data):
            half = len(data) // 2
            written = self._f.write(data[:half])
            self._f.flush()
            half_written.set()
            resume.wait(timeout=5)
            return written + self._f.write(data[half:])

    def open_(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if (armed["on"] and threading.current_thread() is writer
                and set(mode) & set("wax+") and _is_target(file, target)):
            armed["on"] = False
            return _StopsHalfway(f)
        return f

    errors: list[BaseException] = []

    def run_write() -> None:
        try:
            write()
        except BaseException as exc:  # noqa: BLE001 — the test reports any failure
            errors.append(exc)

    writer = threading.Thread(target=run_write)
    monkeypatch.setattr(io, "open", open_)
    writer.start()
    try:
        assert half_written.wait(timeout=5), "the write never reached the record"
        seen = read()
    finally:
        resume.set()
        writer.join(timeout=5)
    assert errors == []
    return seen


def _manifest(ws: Path, run_id: str = "r1") -> Path:
    return ws / "workflows-runs" / "wf" / f"{run_id}.json"


def _running(ws: Path, **fields) -> None:
    run_log.start_run(ws, "wf", "r1", root_session_key="s", started_at=100.0)
    if fields:
        rec = run_log.read_manifest(ws, "wf", "r1")
        rec.update(fields)
        _manifest(ws).write_text(json.dumps(rec), encoding="utf-8")


def _paused(ws: Path) -> None:
    _running(ws, status="needs_input", needs_input_node="ask", final_output="Which one?",
             runs=[{"node_id": "plan", "iteration": 1}])


def _read(ws: Path):
    return lambda: run_log.read_manifest(ws, "wf", "r1")


def _start_run_again(ws):
    _paused(ws)
    return _manifest(ws), lambda: run_log.start_run(
        ws, "wf", "r1", root_session_key="s", started_at=200.0, resumed=True), _read(ws)


def _update_run(ws):
    _running(ws)
    result = WorkflowResult(status="running", final_output=None,
                            runs=[NodeRun(node_id="plan", iteration=1, output="o")])
    return _manifest(ws), lambda: run_log.update_run(ws, "wf", "r1", result), _read(ws)


def _mark_node_started(ws):
    _running(ws)
    return _manifest(ws), lambda: run_log.mark_node_started(
        ws, "wf", "r1", node_id="plan", label="Plan", started_at=150.0), _read(ws)


def _finalize_run(ws):
    _running(ws)
    result = WorkflowResult(status="completed", final_output="done", runs=[], run_id="r1")
    return _manifest(ws), lambda: run_log.finalize_run(
        ws, "wf", result, root_session_key="s", started_at=100.0, finished_at=300.0), _read(ws)


def _finalize_short_circuit(ws):
    _paused(ws)
    return _manifest(ws), lambda: run_log.finalize_short_circuit(
        ws, "wf", "r1", status="cancelled", final_output="Which one?"), _read(ws)


def _claim_for_resume(ws):
    _paused(ws)
    return _manifest(ws), lambda: run_log.claim_for_resume(ws, "wf", "r1"), _read(ws)


def _release_resume_claim(ws):
    _paused(ws)
    paused = run_log.read_manifest(ws, "wf", "r1")
    claimed = run_log.claim_for_resume(ws, "wf", "r1")
    return _manifest(ws), lambda: run_log.release_resume_claim(
        ws, "wf", "r1", prior=paused, claimed=claimed), _read(ws)


def _reconcile_running(ws):
    _running(ws, owner=DEAD_OWNER)
    return _manifest(ws), lambda: run_log.reconcile_running(
        ws, now=time.time(), max_age_s=run_log.RECONCILE_AGE_S), _read(ws)


def _reconcile_one(ws):
    _running(ws, owner=DEAD_OWNER)
    return _manifest(ws), lambda: run_log.reconcile_one(ws, "wf", "r1"), _read(ws)


def _advance_cursor(ws):
    run_log.advance_cursor(ws, "wf", 10.0)
    return (ws / "workflows-runs" / "wf" / ".cursor.json",
            lambda: run_log.advance_cursor(ws, "wf", 20.0),
            lambda: run_log.read_cursor(ws, "wf"))


def _write_pending_again(ws):
    _write_pending(ws, "wf", rec_id="first", kind="prompt", baseline_rate=0.5, target_id="plan")
    return (ws / "workflows-runs" / "wf" / ".pending_validation.json",
            lambda: _write_pending(ws, "wf", rec_id="second", kind="prompt",
                                   baseline_rate=0.25, target_id="plan"),
            lambda: _read_pending(ws, "wf"))


def _provenance_record(ws):
    work = ws / "work"
    work.mkdir()
    provenance.record(work, "a.json", {"run_id": "r0"})
    return (work / provenance.FILENAME,
            lambda: provenance.record(work, "b.json", {"run_id": "r1"}),
            lambda: provenance.load(work))


def _provenance_drop(ws):
    work = ws / "work"
    work.mkdir()
    provenance.record(work, "a.json", {"run_id": "r0"})
    provenance.record(work, "b.json", {"run_id": "r1"})
    return (work / provenance.FILENAME,
            lambda: provenance.drop(work, "a.json"),
            lambda: provenance.load(work))


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(_start_run_again, id="start_run"),
        pytest.param(_update_run, id="update_run"),
        pytest.param(_mark_node_started, id="mark_node_started"),
        pytest.param(_finalize_run, id="finalize_run"),
        pytest.param(_finalize_short_circuit, id="finalize_short_circuit"),
        pytest.param(_claim_for_resume, id="claim_for_resume"),
        pytest.param(_release_resume_claim, id="release_resume_claim"),
        pytest.param(_reconcile_running, id="reconcile_running"),
        pytest.param(_reconcile_one, id="reconcile_one"),
        pytest.param(_advance_cursor, id="advance_cursor"),
        pytest.param(_write_pending_again, id="pending_validation"),
        pytest.param(_provenance_record, id="provenance_record"),
        pytest.param(_provenance_drop, id="provenance_drop"),
    ],
)
def test_a_reader_mid_write_sees_the_old_record_or_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setup
) -> None:
    target, write, read = setup(tmp_path)
    old = read()
    seen = _read_mid_write(monkeypatch, target, write, read)
    new = read()
    assert old != new, "the write under test changed nothing"
    assert seen in (old, new)


def test_a_running_run_stays_live_while_its_manifest_is_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder pruner spares the working folders of the runs live_run_ids
    names. A run that dropped out of that set mid-rewrite could lose its folder
    while it is still running."""
    target, write, _ = _update_run(tmp_path)
    seen = _read_mid_write(monkeypatch, target, write, lambda: run_log.live_run_ids(tmp_path))
    assert seen == {"r1"}
