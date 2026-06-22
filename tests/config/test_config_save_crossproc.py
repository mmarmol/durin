"""Cross-process safety tests for config save.

Verifies that two processes performing disjoint-section edits via
mutate_config() both survive — i.e. neither edit is silently lost to a
last-writer-wins race.  The test spawns real OS processes (multiprocessing
"spawn" context) so the advisory flock is exercised end-to-end.

The last-writer-wins residual of bare load→edit→save_config (not routed
through mutate_config) is documented by a deterministic in-process test that
simulates two stale snapshots: the second save drops the first's edit.

Cross-process lock ordering: load→mutate→save must be wrapped by
cross_process_lock to prevent last-writer-wins corruption.
"""

from __future__ import annotations

import multiprocessing
import os
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


def test_bare_save_loses_one_edit(config_home: Path) -> None:
    """Documents last-writer-wins behavior of bare load→edit→save_config.

    Simulates two "processes" that each loaded a stale snapshot of the same
    on-disk config (before either wrote back), then each saved a disjoint
    edit.  The second save stomps the first's change — deterministic,
    in-process, no race required.

    This documents the accepted residual: a direct load→save_config sequence
    NOT routed through mutate_config is last-writer-wins.  mutate_config
    closes this via locked reload inside the lock (see the companion test
    test_mutate_config_both_edits_survive).

    This documents the accepted residual of bare load→save_config not
    routed through mutate_config (last-writer-wins).
    """
    os.environ["DURIN_HOME"] = str(config_home)
    from durin.config.loader import load_config, save_config

    # Both "processes" read the same initial on-disk state.
    snap_a = load_config(config_home / "config.json")
    snap_b = load_config(config_home / "config.json")

    # Each mutates a disjoint section on its stale snapshot.
    snap_a.agents.defaults.model = "glm-5.1"
    snap_b.providers.zhipu.api_key = "sk-test-123"

    # First writer commits — second writer overwrites with its stale snapshot.
    save_config(snap_a, config_home / "config.json")
    save_config(snap_b, config_home / "config.json")

    # Reload from disk: snap_b's save was last → snap_a's model edit is lost.
    reloaded = load_config(config_home / "config.json")
    assert reloaded.providers.zhipu.api_key == "sk-test-123", (
        "second saver's edit should survive (last-writer-wins)"
    )
    assert reloaded.agents.defaults.model != "glm-5.1", (
        "first saver's edit should be lost — bare save is last-writer-wins"
    )
