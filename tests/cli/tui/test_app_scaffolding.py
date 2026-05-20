"""Smoke tests for the Textual TUI scaffolding (D5.1).

Subsequent sub-tasks (D5.2–D5.10) layer widgets, streaming, and modals
on top of this skeleton. This file only asserts the App constructs,
mounts a banner placeholder, and quits cleanly on Ctrl+Q.
"""

from __future__ import annotations

import pytest

from durin.cli.tui.app import DurinApp


def test_app_constructs_without_agent_loop() -> None:
    app = DurinApp(agent_loop=None)
    assert app.TITLE.startswith("durin")
    assert "ctrl+q" in [b[0] if isinstance(b, tuple) else b.key for b in app.BINDINGS]


@pytest.mark.asyncio
async def test_app_runs_and_quits_on_ctrl_q() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        # The banner mounts somewhere in the tree.
        assert app.query_one("#banner")
        await pilot.press("ctrl+q")
    # If we get here the app exited cleanly under the Ctrl+Q binding.


@pytest.mark.asyncio
async def test_app_carries_workspace_routing_context() -> None:
    """Future sub-tasks need cli_channel + cli_chat_id stored on the App."""
    app = DurinApp(agent_loop=None, cli_channel="cli", cli_chat_id="my-session")
    async with app.run_test():
        assert app._cli_channel == "cli"
        assert app._cli_chat_id == "my-session"
