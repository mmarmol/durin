"""A turn with input from an API token carries no person's authority.

In a webui conversation a person is reachable, so a privileged request
(installing an MCP server, importing or editing a skill, an exec command past
the policy) is put to that person in the chat. A message sent with a
``chat:write`` token comes from a program: the turn it opens or joins must not
ask in the chat. Its requests take the path of a context with no person
instead: filed as pending for ``durin approvals``, or refused for exec.
"""

from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path
from types import SimpleNamespace

import pytest

from durin.agent import approval, pending_answers
from durin.agent.approval_prompt import ChatHandles


@pytest.fixture(autouse=True)
def _consumer(monkeypatch):
    monkeypatch.setattr(pending_answers, "_CONSUMER_ACTIVE", True)


class _Sessions:
    def __init__(self):
        self.s = SimpleNamespace(metadata={})

    def get_or_create(self, key):
        return self.s

    def save(self, session, **kw):
        pass


_WEBUI = SimpleNamespace(channel="websocket", chat_id="abc", session_key="websocket:abc")


def _asker():
    return ChatHandles(sessions=_Sessions(), timeout_s=5).asker(_WEBUI)


@pytest.mark.asyncio
async def test_a_person_s_turn_is_asked_in_the_chat() -> None:
    async def _turn():
        approval.note_turn_input({"webui": True})
        return _asker(), approval.turn_has_api_input(), approval.can_authorize("websocket:abc")

    asker, api_input, can_authorize = await asyncio.create_task(_turn())
    assert asker is not None
    assert api_input is False
    assert can_authorize is True


@pytest.mark.asyncio
async def test_a_turn_with_api_input_is_never_asked_in_the_chat() -> None:
    async def _turn():
        approval.note_turn_input({"webui": True, "origin": "api"})
        return _asker(), approval.turn_has_api_input(), approval.can_authorize("websocket:abc")

    asker, api_input, can_authorize = await asyncio.create_task(_turn())
    assert asker is None
    assert api_input is True
    assert can_authorize is False


@pytest.mark.asyncio
async def test_api_input_marks_only_its_own_turn() -> None:
    async def _api_turn():
        approval.note_turn_input({"origin": "api"})

    async def _person_turn():
        return _asker()

    await asyncio.create_task(_api_turn())
    assert (await asyncio.create_task(_person_turn())) is not None


def test_api_input_does_not_change_whether_the_agent_may_wait_for_an_answer() -> None:
    # ask_user may still block: the API client answers with a plain message.
    # Run in a copied context so the flag cannot leak into later tests.
    def _turn() -> tuple[bool, bool]:
        approval.note_turn_input({"origin": "api"})
        return (approval.human_reachable("websocket:abc"),
                pending_answers.can_block("websocket:abc"))

    assert contextvars.copy_context().run(_turn) == (True, True)


@pytest.mark.asyncio
async def test_the_legacy_gate_stages_under_api_input_and_says_why(tmp_path: Path) -> None:
    # ``gate`` stays until its last caller is gone; it keeps the same rule.
    async def _turn():
        approval.note_turn_input({"webui": True, "origin": "api"})
        return approval.gate(tmp_path, "mcp", action="install", summary="install a server",
                             session_key="websocket:abc")

    decision = await asyncio.create_task(_turn())
    assert decision.allow is False
    assert decision.staged is True
    assert "API token" in decision.message
