"""Unit tests for CommandsService."""

from __future__ import annotations

import pytest

from durin.service.commands import CommandsListQuery, CommandsService
from durin.service.principal import Principal


@pytest.fixture()
def svc():
    return CommandsService()


@pytest.fixture()
def local():
    return Principal.local()


async def test_commands_list_returns_palette(svc, local, monkeypatch):
    monkeypatch.setattr(
        "durin.command.builtin.builtin_command_palette",
        lambda: [{"name": "/help", "description": "Show help"}],
    )
    result = await svc.list(CommandsListQuery(), local)
    assert isinstance(result.commands, list)
    assert result.commands[0]["name"] == "/help"


async def test_commands_list_returns_nonempty_by_default(svc, local):
    result = await svc.list(CommandsListQuery(), local)
    assert isinstance(result.commands, list)
    assert len(result.commands) > 0
