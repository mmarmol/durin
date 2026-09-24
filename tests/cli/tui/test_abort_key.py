"""Esc cancels the TUI's turn under the key the loop registered it with."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from durin.cli.tui.app import DurinApp


@pytest.mark.asyncio
async def test_abort_cancels_under_the_bus_turn_key() -> None:
    cancel = AsyncMock(return_value=1)
    fake = SimpleNamespace(
        _agent_loop=SimpleNamespace(
            _cancel_active_tasks=cancel,
            bus_turn_key=lambda key: "unified:default",
        ),
        _cli_channel="cli",
        _cli_chat_id="direct",
        _current_assistant_bubble=object(),
        _turn_started_at=1.0,
        _dismiss_working_indicator=lambda: None,
        _end_turn_diagnostics=lambda: None,
    )

    await DurinApp.action_abort(fake)

    cancel.assert_awaited_once_with("unified:default")
