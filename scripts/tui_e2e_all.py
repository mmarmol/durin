#!/usr/bin/env python
"""E2E smoke test for all TUI features.

Creates a minimal mock agent loop so features that need it
(model picker, sidebar) can be tested.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from durin.cli.tui.app import DurinApp  # noqa: E402
from durin.cli.tui.probe import screen_text  # noqa: E402


def _mock_agent_loop(workspace: Path) -> SimpleNamespace:
    """Minimal mock with attributes the TUI features need."""
    return SimpleNamespace(
        workspace=workspace,
        model_presets={
            "default": SimpleNamespace(
                model="glm-5.2",
                provider="auto",
                reasoning_effort=None,
            ),
        },
        _active_preset="default",
        _mcp_servers={},
        _mcp_connected=False,
        _mcp_stacks={},
        sessions=SimpleNamespace(
            get_or_create=lambda key: SimpleNamespace(metadata={}),
        ),
    )


async def _check(pilot, label: str, expected_substrings: list[str]) -> bool:
    await pilot.pause(delay=0.3)
    text = screen_text(pilot.app)
    missing = [s for s in expected_substrings if s.lower() not in text.lower()]
    status = "✓" if not missing else "✗"
    print(f"  {status} {label}")
    if missing:
        print(f"    Missing: {missing}")
        # Print screen for debugging
        for line in text.splitlines()[:15]:
            print(f"    | {line.rstrip()}")
    return not missing


async def _run() -> int:
    workspace = Path.cwd()
    loop = _mock_agent_loop(workspace)
    app = DurinApp(agent_loop=loop)
    results: list[bool] = []

    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.pause(delay=0.5)
        print("E2E TUI Feature Tests\n" + "=" * 60)

        # 1. Boot
        results.append(await _check(pilot, "Boot", ["durin", "message"]))

        # 2. Model picker (Ctrl+L)
        await pilot.press("ctrl+l")
        await pilot.pause(delay=0.5)
        await pilot.pause(delay=0.5)
        results.append(await _check(pilot, "Model picker (Ctrl+L)", ["model"]))
        await pilot.press("escape")
        await pilot.pause(delay=0.3)

        # 3. Command palette (Ctrl+P)
        await pilot.press("ctrl+p")
        await pilot.pause(delay=0.5)
        await pilot.pause(delay=0.5)
        results.append(await _check(pilot, "Command palette (Ctrl+P)", ["search", "command"]))
        await pilot.press("escape")
        await pilot.pause(delay=0.3)

        # 4. Variant picker (Ctrl+Shift+L)
        await pilot.press("ctrl+shift+l")
        await pilot.pause(delay=0.5)
        await pilot.pause(delay=0.5)
        results.append(await _check(pilot, "Variant picker (Ctrl+Shift+L)", ["effort", "low", "medium", "high"]))
        await pilot.press("escape")
        await pilot.pause(delay=0.3)

        # 5. Sidebar panels (Ctrl+B)
        await pilot.press("ctrl+b")
        await pilot.pause(delay=0.5)
        await pilot.pause(delay=0.5)
        results.append(await _check(pilot, "Sidebar (Ctrl+B)", ["todo", "files", "mcp"]))
        await pilot.press("ctrl+b")  # toggle off
        await pilot.pause(delay=0.3)

        # 6. Diff viewer (Ctrl+G)
        await pilot.press("ctrl+g")
        await pilot.pause(delay=0.5)
        await pilot.pause(delay=0.5)
        results.append(await _check(pilot, "Diff viewer (Ctrl+G)", ["changed", "file"]))
        await pilot.press("escape")
        await pilot.pause(delay=0.3)

        # 7. Prompt history (type then Up arrow)
        await pilot.press("a", "b", "c", "enter")
        await pilot.pause(delay=0.3)
        await pilot.press("up")
        await pilot.pause(delay=0.3)
        results.append(await _check(pilot, "Prompt history (Up arrow)", ["abc"]))

    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Results: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
