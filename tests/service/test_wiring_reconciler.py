"""Periodic run-manifest reconciliation (ghost-run prevention)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


def test_periodic_reconciler_flips_dead_owner_run(tmp_path, monkeypatch):
    import durin.service.wiring as wiring
    from durin.workflow import run_log

    monkeypatch.setattr(wiring, "_reconciler_started", type(wiring._reconciler_started)())
    run_log.start_run(tmp_path, "wf", "ghost", root_session_key="s",
                      started_at=time.time())
    f = tmp_path / "workflows-runs" / "wf" / "ghost.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec["owner"] = {"pid": 2**22 + 4242, "started": "never"}
    f.write_text(json.dumps(rec), encoding="utf-8")

    # Wait for the sweep that flips the run, then read the manifest once: a
    # read taken while the sweep's thread is rewriting it would test the
    # timing of two threads, not the reconciler.
    flipped = threading.Event()
    real_reconcile = run_log.reconcile_running

    def reconcile_and_report(*args, **kwargs):
        count = real_reconcile(*args, **kwargs)
        if count:
            flipped.set()
        return count

    monkeypatch.setattr(run_log, "reconcile_running", reconcile_and_report)

    assert wiring.start_periodic_run_reconciler(
        lambda: Path(tmp_path), period_s=0.2) is True
    # Second start is a no-op (once per process).
    assert wiring.start_periodic_run_reconciler(
        lambda: Path(tmp_path), period_s=0.2) is False

    assert flipped.wait(timeout=10), "no periodic sweep flipped the dead-owner run"
    assert run_log.read_manifest(tmp_path, "wf", "ghost")["status"] == "crashed"
