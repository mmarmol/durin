"""Cross-process safety tests for tui-state.json.

Two processes each append a DISTINCT entry concurrently to the same
tui-state.json (one adds a model, one adds a prompt). Without
cross_process_lock wrapping load→mutate→save, one write can overwrite
the other. With the lock, both entries must survive.

See docs/architecture/concurrency.md for lock-ordering invariants.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path


def _add_model(state_dir: str, model: str) -> None:
    os.environ["DURIN_HOME"] = state_dir
    from durin.cli.tui import state  # noqa: PLC0415

    state._state_dir = Path(state_dir)
    state.add_recent_model(model)


def _add_prompt(state_dir: str, text: str) -> None:
    os.environ["DURIN_HOME"] = state_dir
    from durin.cli.tui import state  # noqa: PLC0415

    state._state_dir = Path(state_dir)
    state.add_prompt(text)


def test_concurrent_model_and_prompt_both_survive(tmp_path: Path) -> None:
    """Concurrent add_recent_model + add_prompt must not lose either entry."""
    ctx = mp.get_context("spawn")
    ps = [
        ctx.Process(target=_add_model, args=(str(tmp_path), "model-A")),
        ctx.Process(target=_add_prompt, args=(str(tmp_path), "prompt-B")),
    ]
    for p in ps:
        p.start()
    for p in ps:
        p.join(20)

    from durin.cli.tui import state  # noqa: PLC0415

    state._state_dir = tmp_path
    models = state.get_recent_models()
    history = state.get_prompt_history()
    assert "model-A" in models, f"model-A lost — got {models}"
    assert "prompt-B" in history, f"prompt-B lost — got {history}"


def test_concurrent_two_model_appends_both_survive(tmp_path: Path) -> None:
    """Two concurrent add_recent_model calls with distinct models must both survive."""
    ctx = mp.get_context("spawn")
    ps = [
        ctx.Process(target=_add_model, args=(str(tmp_path), f"model-{i}"))
        for i in range(2)
    ]
    for p in ps:
        p.start()
    for p in ps:
        p.join(20)

    from durin.cli.tui import state  # noqa: PLC0415

    state._state_dir = tmp_path
    models = state.get_recent_models()
    assert "model-0" in models, f"model-0 lost — got {models}"
    assert "model-1" in models, f"model-1 lost — got {models}"
