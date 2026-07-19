"""Periodic run-manifest reconciliation (ghost-run prevention)."""
from __future__ import annotations

import json
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

    assert wiring.start_periodic_run_reconciler(
        lambda: Path(tmp_path), period_s=0.2) is True
    # Second start is a no-op (once per process).
    assert wiring.start_periodic_run_reconciler(
        lambda: Path(tmp_path), period_s=0.2) is False

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if run_log.read_manifest(tmp_path, "wf", "ghost")["status"] == "crashed":
            break
        time.sleep(0.1)
    assert run_log.read_manifest(tmp_path, "wf", "ghost")["status"] == "crashed"
