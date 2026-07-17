"""Tests for the gateway-side dream worker supervision."""

from __future__ import annotations

import sys
import textwrap
import threading
import time

import pytest


@pytest.fixture()
def fake_worker(tmp_path, monkeypatch):
    """Route the supervisor at a controllable stand-in worker script.

    Returns a function(body: str) that installs a fake worker whose __main__
    body is *body* (dedented); the script receives the real worker argv tail.
    """
    import durin.memory.dream_supervisor as sup

    def install(body: str):
        script = tmp_path / "fake_worker.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        monkeypatch.setattr(
            sup, "_worker_argv",
            lambda mode, trigger: [sys.executable, str(script), mode, trigger],
        )
        return script

    return install


@pytest.fixture()
def no_invalidate(monkeypatch):
    """Record alias-cache invalidations instead of touching the real cache."""
    calls: list = []
    import durin.memory.aliases_cache as aliases_cache

    monkeypatch.setattr(
        aliases_cache, "invalidate_alias_index", lambda root: calls.append(root)
    )
    return calls


def test_progress_forwarded_in_order_and_code_returned(fake_worker, no_invalidate, tmp_path):
    from durin.memory.dream_supervisor import run_dream_worker

    fake_worker(
        """
        import json, sys
        print(json.dumps({"kind": "run_started"}), flush=True)
        print(json.dumps({"kind": "activity", "item": {"n": 1}}), flush=True)
        print(json.dumps({"kind": "run_finished", "ok": True}), flush=True)
        """
    )
    events: list[dict] = []
    code, err = run_dream_worker(
        workspace=tmp_path, mode="full", trigger="cron", on_progress=events.append
    )
    assert code == 0
    assert [e["kind"] for e in events] == ["run_started", "activity", "run_finished"]


def test_synthesized_run_finished_on_silent_failure(fake_worker, no_invalidate, tmp_path):
    from durin.memory.dream_supervisor import run_dream_worker

    fake_worker(
        """
        import sys
        sys.stderr.write("boom traceback here\\n")
        sys.exit(1)
        """
    )
    events: list[dict] = []
    code, err = run_dream_worker(
        workspace=tmp_path, mode="full", trigger="cron", on_progress=events.append
    )
    assert code == 1
    assert "boom traceback" in err
    assert events[-1] == {"kind": "run_finished", "ok": False}


def test_non_protocol_stdout_lines_skipped(fake_worker, no_invalidate, tmp_path):
    from durin.memory.dream_supervisor import run_dream_worker

    fake_worker(
        """
        import json
        print("not json at all", flush=True)
        print(json.dumps({"kind": "run_finished", "ok": True}), flush=True)
        """
    )
    events: list[dict] = []
    code, _err = run_dream_worker(
        workspace=tmp_path, mode="full", trigger="cron", on_progress=events.append
    )
    assert code == 0
    assert [e["kind"] for e in events] == ["run_finished"]


def test_alias_cache_invalidated_after_run(fake_worker, no_invalidate, tmp_path):
    from durin.memory.dream_supervisor import run_dream_worker

    fake_worker(
        """
        import json
        print(json.dumps({"kind": "run_finished", "ok": True}), flush=True)
        """
    )
    run_dream_worker(
        workspace=tmp_path, mode="full", trigger="cron", on_progress=lambda p: None
    )
    assert no_invalidate == [tmp_path / "memory"]


def test_stop_terminates_running_worker(fake_worker, no_invalidate, tmp_path):
    from durin.memory.dream_supervisor import run_dream_worker, stop_dream_workers

    fake_worker(
        """
        import json, time
        print(json.dumps({"kind": "run_started"}), flush=True)
        time.sleep(60)
        """
    )
    result: dict = {}

    def runner():
        code, _ = run_dream_worker(
            workspace=tmp_path, mode="full", trigger="cron",
            on_progress=lambda p: None,
        )
        result["code"] = code

    t = threading.Thread(target=runner)
    t.start()
    deadline = time.monotonic() + 10
    from durin.memory import dream_supervisor as sup

    while time.monotonic() < deadline and not sup._running_procs:
        time.sleep(0.05)
    assert sup._running_procs, "worker never registered as running"
    stop_dream_workers(grace_s=2)
    t.join(timeout=15)
    assert not t.is_alive()
    assert result["code"] != 0
