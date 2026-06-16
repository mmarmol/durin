"""CommandsService — built-in command palette.

Extracted from ``durin/channels/websocket.py`` (``_handle_commands``) in SP1.
Trivial: delegates directly to ``builtin_command_palette()``.

Escape hatch: ``CommandsResult.commands`` is ``list[Any]`` — the palette items
are plain dicts built by ``builtin_command_palette()`` and not modelled here
(SP3 can tighten).
"""

from __future__ import annotations

from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Query, Result


class CommandsListQuery(Query):
    """No inputs."""


class CommandsResult(Result):
    commands: list[Any]


class CommandsService:
    """Return the built-in command palette."""

    @route(
        "GET",
        "/api/commands",
        scope=Scope.SYSTEM_READ.value,
        request_model=CommandsListQuery,
        response_model=CommandsResult,
        summary="List built-in command palette entries",
    )
    async def list(
        self, query: CommandsListQuery, principal: Principal
    ) -> CommandsResult:
        principal.require(Scope.SYSTEM_READ)
        from durin.command.builtin import builtin_command_palette

        return CommandsResult(commands=builtin_command_palette())
