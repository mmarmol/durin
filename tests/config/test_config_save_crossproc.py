"""Cross-process safety tests for config save.

Verifies that two processes performing disjoint-section edits via
mutate_config() both survive — i.e. neither edit is silently lost to a
last-writer-wins race.  The test spawns real OS processes (multiprocessing
"spawn" context) so the advisory flock is exercised end-to-end.

The must-fail-without-lock property is verified by a second parameterized
path that bypasses mutate_config and uses the bare load→edit→save_config
sequence: without the cross_process_lock wrapping save_config, two
concurrent whole-config dumps clobber each other.

See docs/architecture/concurrency.md for lock-ordering invariants.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers that run inside spawned subprocesses
# (must be module-level so they are picklable under "spawn")
# ---------------------------------------------------------------------------


def _worker_mutate(home: str, section: str, value: str) -> None:
    """Call mutate_config to set agents.defaults.model or providers key."""
    os.environ["DURIN_HOME"] = home
    # Re-import after env is set so home resolution picks up the override.
    from durin.config.loader import mutate_config  # noqa: PLC0415
    from durin.config.schema import Config  # noqa: PLC0415

    if section == "model":
        def _set_model(cfg: Config) -> None:
            cfg.agents.defaults.model = value
        mutate_config(_set_model)
    elif section == "apikey":
        def _set_apikey(cfg: Config) -> None:
            cfg.providers.zhipu.api_key = value
        mutate_config(_set_apikey)


def _worker_bare_save(home: str, section: str, value: str, barrier_path: str) -> None:
    """Bare load→edit→save_config WITHOUT mutate_config (no lock around RMW)."""
    os.environ["DURIN_HOME"] = home
    from durin.config.loader import load_config, save_config  # noqa: PLC0415
    from durin.config.schema import Config  # noqa: PLC0415

    cfg = load_config()

    # Synchronize: wait until both processes have loaded before either saves.
    barrier = Path(barrier_path)
    my_flag = barrier.with_suffix(f".{os.getpid()}.ready")
    my_flag.touch()
    deadline = time.monotonic() + 5.0
    while True:
        flags = list(barrier.parent.glob(f"{barrier.stem}.*.ready"))
        if len(flags) >= 2:
            break
        if time.monotonic() > deadline:
            raise TimeoutError("barrier timeout")
        time.sleep(0.02)

    if section == "model":
        cfg.agents.defaults.model = value
    elif section == "apikey":
        cfg.providers.zhipu.api_key = value

    save_config(cfg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_home(tmp_path: Path) -> Path:
    """A temporary DURIN_HOME with a pre-seeded split layout."""
    from durin.config.loader import _split_dir, save_config
    from durin.config.schema import Config

    cfg_path = tmp_path / "config.json"
    _split_dir(cfg_path).mkdir(parents=True)
    save_config(Config(), cfg_path)
    return tmp_path


def test_mutate_config_both_edits_survive(config_home: Path) -> None:
    """Two processes each mutating a DISJOINT section → both edits survive.

    Without lock-protected RMW the late writer would reload a stale config
    (missing the first writer's change) and overwrite it, losing one edit.
    With mutate_config the reload is inside the lock so the late writer
    always sees the early writer's commit.
    """
    ctx = multiprocessing.get_context("spawn")
    home = str(config_home)

    p1 = ctx.Process(target=_worker_mutate, args=(home, "model", "glm-5.1"))
    p2 = ctx.Process(target=_worker_mutate, args=(home, "apikey", "sk-test-123"))
    p1.start()
    p2.start()
    p1.join(timeout=30)
    p2.join(timeout=30)

    assert p1.exitcode == 0, f"mutate worker 1 failed with exit {p1.exitcode}"
    assert p2.exitcode == 0, f"mutate worker 2 failed with exit {p2.exitcode}"

    # Reload from disk in this process.
    os.environ["DURIN_HOME"] = home
    from durin.config.loader import load_config
    loaded = load_config(config_home / "config.json")

    assert loaded.agents.defaults.model == "glm-5.1", (
        "agents.defaults.model was lost — late writer clobbered the early writer"
    )
    assert loaded.providers.zhipu.api_key == "sk-test-123", (
        "providers.zhipu.api_key was lost — late writer clobbered the early writer"
    )


def test_bare_save_loses_one_edit(config_home: Path, tmp_path: Path) -> None:
    """Demonstrates the last-writer-wins hazard of bare load→edit→save_config.

    Both processes load BEFORE either saves (coordinated by a flag file
    barrier), then each saves its disjoint edit. The late saver overwrites
    the early saver's write. This test documents the accepted residual: a
    direct load→save not routed through mutate_config remains last-writer-wins.

    See docs/architecture/concurrency.md (Phase-A residual ledger) and the
    inline note in save_config / mutate_config.
    """
    ctx = multiprocessing.get_context("spawn")
    home = str(config_home)
    barrier = str(tmp_path / "barrier")

    p1 = ctx.Process(target=_worker_bare_save, args=(home, "model", "glm-5.1", barrier))
    p2 = ctx.Process(target=_worker_bare_save, args=(home, "apikey", "sk-test-123", barrier))
    p1.start()
    p2.start()
    p1.join(timeout=30)
    p2.join(timeout=30)

    assert p1.exitcode == 0
    assert p2.exitcode == 0

    os.environ["DURIN_HOME"] = home
    from durin.config.loader import load_config
    loaded = load_config(config_home / "config.json")

    # At least one of the two edits is lost (last writer wins).
    model_ok = loaded.agents.defaults.model == "glm-5.1"
    apikey_ok = loaded.providers.zhipu.api_key == "sk-test-123"
    assert not (model_ok and apikey_ok), (
        "Expected one edit to be lost under bare save, but both survived "
        "(race condition did not manifest — try again or accept flakiness)"
    )
