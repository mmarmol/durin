"""Gateway supervision of the standing embedding server."""
from __future__ import annotations

import sys
import textwrap
import time
from types import SimpleNamespace

import pytest


def _cfg(*, enabled=True, isolation="service", port=0, cap=0):
    return SimpleNamespace(memory=SimpleNamespace(
        enabled=enabled,
        embedding=SimpleNamespace(
            isolation=isolation, service_port=port, service_max_rss_mb=cap),
    ))


@pytest.fixture(autouse=True)
def _reset_supervisor_state():
    import durin.memory.embed_supervisor as sup

    sup.stop_embed_server(grace_s=1.0)
    sup._proc = None
    yield
    sup.stop_embed_server(grace_s=1.0)
    sup._proc = None


@pytest.fixture()
def fake_server(tmp_path, monkeypatch):
    """Route the supervisor at a stand-in server script."""
    import durin.memory.embed_supervisor as sup

    monkeypatch.setenv("DURIN_HOME", str(tmp_path))

    def install(body: str):
        script = tmp_path / "fake_embed_server.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        monkeypatch.setattr(
            sup, "_server_argv", lambda port: [sys.executable, str(script)])
        return script

    return install


def test_disabled_configs_do_not_start():
    from durin.memory.embed_supervisor import start_embed_server_supervisor

    assert start_embed_server_supervisor(_cfg(enabled=False)) is False
    assert start_embed_server_supervisor(_cfg(isolation="process")) is False


def test_spawns_and_respawns_after_death(fake_server, monkeypatch):
    import durin.memory.embed_supervisor as sup

    monkeypatch.setattr(sup, "_RESPAWN_BACKOFF_S", 0.2)
    monkeypatch.setattr(sup, "_WATCHDOG_INTERVAL_S", 0.2)
    monkeypatch.setattr(sup, "_FAST_EXIT_S", 0.0)   # nothing counts as fast
    fake_server("""
        import time
        time.sleep(120)
    """)
    assert sup.start_embed_server_supervisor(_cfg()) is True

    deadline = time.monotonic() + 10
    first = None
    while time.monotonic() < deadline:
        with sup._state_lock:
            proc = sup._proc
        if proc is not None and proc.poll() is None:
            first = proc
            break
        time.sleep(0.05)
    assert first is not None, "server never spawned"

    first.kill()
    deadline = time.monotonic() + 15
    respawned = None
    while time.monotonic() < deadline:
        with sup._state_lock:
            proc = sup._proc
        if proc is not None and proc.pid != first.pid and proc.poll() is None:
            respawned = proc
            break
        time.sleep(0.05)
    assert respawned is not None, "server was not respawned after death"


def test_gives_up_after_repeated_instant_exits(fake_server, monkeypatch):
    import durin.memory.embed_supervisor as sup

    monkeypatch.setattr(sup, "_RESPAWN_BACKOFF_S", 0.1)
    monkeypatch.setattr(sup, "_WATCHDOG_INTERVAL_S", 0.1)
    fake_server("""
        import sys
        sys.exit(1)
    """)
    assert sup.start_embed_server_supervisor(_cfg()) is True

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        threads = [t.name for t in __import__("threading").enumerate()]
        if "embed-server-supervisor" not in threads:
            break
        time.sleep(0.1)
    threads = [t.name for t in __import__("threading").enumerate()]
    assert "embed-server-supervisor" not in threads, "supervisor never gave up"


def test_stop_terminates_server_and_clears_discovery(fake_server, tmp_path, monkeypatch):
    import durin.memory.embed_supervisor as sup
    from durin.memory.embed_server import write_discovery

    monkeypatch.setattr(sup, "_WATCHDOG_INTERVAL_S", 0.2)
    fake_server("""
        import time
        time.sleep(120)
    """)
    assert sup.start_embed_server_supervisor(_cfg()) is True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with sup._state_lock:
            proc = sup._proc
        if proc is not None and proc.poll() is None:
            break
        time.sleep(0.05)
    write_discovery(port=1, token="t", model="m")

    sup.stop_embed_server(grace_s=2.0)
    assert proc.wait(timeout=10) is not None
    assert not (tmp_path / "embed-server.json").exists()
