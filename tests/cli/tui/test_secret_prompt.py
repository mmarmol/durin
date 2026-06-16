"""Tests for SecretPromptScreen — the masked credential prompt.

The screen writes the value straight to the SecretStore; the value is
never returned to the caller, only a True/False stored/cancelled flag.
"""

from __future__ import annotations

import pytest

from durin.cli.tui.app import DurinApp
from durin.cli.tui.screens.secret_prompt import SecretPromptScreen


@pytest.mark.asyncio
async def test_secret_prompt_stores_value(monkeypatch, tmp_path) -> None:
    secrets_path = tmp_path / "secrets.json"
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path", lambda: secrets_path
    )
    app = DurinApp(agent_loop=None)
    result: list[bool | None] = []
    async with app.run_test() as pilot:
        from textual.widgets import Input

        screen = SecretPromptScreen(
            name="STRIPE_KEY", service="stripe", purpose="charge cards"
        )
        app.push_screen(screen, lambda stored: result.append(stored))
        await pilot.pause()
        screen.query_one("#secret-input", Input).value = "sk_live_abc123"
        await pilot.press("enter")
        await pilot.pause()

    assert result == [True]
    from durin.security.secrets import get_secret_store

    entry = get_secret_store(reload=True).get("STRIPE_KEY")
    assert entry is not None
    assert entry.value == "sk_live_abc123"
    assert entry.service == "stripe"
    assert entry.scope == ["exec"]
    assert entry.origin == "tui"


@pytest.mark.asyncio
async def test_secret_prompt_cancel_stores_nothing(monkeypatch, tmp_path) -> None:
    secrets_path = tmp_path / "secrets.json"
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path", lambda: secrets_path
    )
    app = DurinApp(agent_loop=None)
    result: list[bool | None] = []
    async with app.run_test() as pilot:
        app.push_screen(
            SecretPromptScreen(name="STRIPE_KEY", service="stripe"),
            lambda stored: result.append(stored),
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert result == [False]
    assert not secrets_path.exists()


def test_save_empty_value_does_not_write(monkeypatch, tmp_path) -> None:
    secrets_path = tmp_path / "secrets.json"
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path", lambda: secrets_path
    )
    screen = SecretPromptScreen(name="TUI_TOKEN", service="github")
    # Calling _save directly is safe for the empty-value path:
    # _show_error swallows its own NoMatches; _save returns before any store call.
    screen._save("   ")
    assert not secrets_path.exists()
