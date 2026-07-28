"""Tests for SecretPromptScreen — the masked credential prompt.

The screen writes the value straight to the SecretStore; the value is never
returned to the caller, only the stored entry's metadata (or None when
cancelled) so the caller can describe what landed without guessing.
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
    result: list[object] = []
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

    # The stored entry comes back so the caller can report the real scope
    # instead of assuming one — never the value.
    assert len(result) == 1
    assert result[0].name == "STRIPE_KEY"
    assert result[0].scope == ["exec"]
    assert "sk_live_abc123" not in repr(result[0])
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
    result: list[object] = []
    async with app.run_test() as pilot:
        app.push_screen(
            SecretPromptScreen(name="STRIPE_KEY", service="stripe"),
            lambda stored: result.append(stored),
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert result == [None]
    assert not secrets_path.exists()


@pytest.mark.asyncio
async def test_secret_prompt_replace_returns_the_preserved_scope(
    monkeypatch, tmp_path
) -> None:
    """Replace mode keeps the entry's metadata, so the caller must hear the
    scope that survived — not the one it would have guessed."""
    secrets_path = tmp_path / "secrets.json"
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path", lambda: secrets_path
    )
    from durin.service.secrets import SecretsService

    SecretsService().store_entry(
        name="SLACK_BOT_TOKEN", value="xoxb-first", service="channel:slack",
        scope=["channel:slack"],
    )
    app = DurinApp(agent_loop=None)
    result: list[object] = []
    async with app.run_test() as pilot:
        from textual.widgets import Input

        screen = SecretPromptScreen(
            name="SLACK_BOT_TOKEN", service="channel:slack", update=True
        )
        app.push_screen(screen, lambda stored: result.append(stored))
        await pilot.pause()
        screen.query_one("#secret-input", Input).value = "xoxb-second"
        await pilot.press("enter")
        await pilot.pause()

    assert len(result) == 1
    assert result[0].scope == ["channel:slack"]


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
