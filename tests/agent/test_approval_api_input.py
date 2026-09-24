"""A turn with input from an API token carries no person's authority.

In a webui conversation a person is reachable, so the approval gate lets
privileged actions (installing an MCP server, importing or editing a skill)
run. A message sent with a ``chat:write`` token comes from a program: the turn
it opens or joins must stage those actions for a person to approve instead.
"""

from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path

import pytest

from durin.agent import approval, pending_answers


@pytest.fixture(autouse=True)
def _consumer(monkeypatch):
    monkeypatch.setattr(pending_answers, "_CONSUMER_ACTIVE", True)


def _gate(tmp_path: Path) -> approval.Decision:
    return approval.gate(
        tmp_path, "mcp", action="install", summary="install a server",
        session_key="websocket:abc",
    )


@pytest.mark.asyncio
async def test_a_person_s_turn_is_allowed(tmp_path: Path) -> None:
    async def _turn():
        approval.note_turn_input({"webui": True})
        return _gate(tmp_path)

    assert (await asyncio.create_task(_turn())).allow is True


@pytest.mark.asyncio
async def test_a_turn_with_api_input_is_staged(tmp_path: Path) -> None:
    async def _turn():
        approval.note_turn_input({"webui": True, "origin": "api"})
        return _gate(tmp_path)

    decision = await asyncio.create_task(_turn())
    assert decision.allow is False
    assert decision.staged is True


@pytest.mark.asyncio
async def test_api_input_marks_only_its_own_turn(tmp_path: Path) -> None:
    async def _api_turn():
        approval.note_turn_input({"origin": "api"})

    async def _person_turn():
        return _gate(tmp_path)

    await asyncio.create_task(_api_turn())
    assert (await asyncio.create_task(_person_turn())).allow is True


def test_api_input_does_not_change_whether_the_agent_may_wait_for_an_answer() -> None:
    # ask_user may still block: the API client answers with a plain message.
    # Run in a copied context so the flag cannot leak into later tests.
    def _turn() -> bool:
        approval.note_turn_input({"origin": "api"})
        return approval.human_reachable("websocket:abc")

    assert contextvars.copy_context().run(_turn) is True
