"""``/new`` schedules ``_archive_closed_session`` in the background — neither
of its two best-effort steps (``consolidator.archive()``, then
``write_session_summary()``) may let a failure reach the user or go
unlogged."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.command.builtin import cmd_new
from durin.command.router import CommandContext
from durin.memory.session_summary_store import (
    closed_record_key,
    read_session_summary_entry,
    write_session_summary,
)

KEY = "cli:test"


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")


def _ctx(loop: AgentLoop) -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="u", chat_id="test", content="/new")
    return CommandContext(msg=msg, session=None, key=KEY, raw="/new", loop=loop)


@pytest.mark.asyncio
async def test_archive_failure_still_starts_new_session_and_files_prior_summary(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """``consolidator.archive()`` raising must not stop ``/new`` from
    answering, and the prior consolidated summary must still be filed as
    the closed record."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create(KEY)
    session.add_message("user", "hello")
    session.add_message("assistant", "hi")
    loop.sessions.save(session)
    last_active = session.updated_at

    write_session_summary(loop.workspace, KEY, "- prior consolidated notes", last_active=last_active)

    loop.consolidator.archive = AsyncMock(side_effect=RuntimeError("archive boom"))

    with caplog.at_level(logging.ERROR, logger="durin.command.builtin"):
        result = await cmd_new(_ctx(loop))
        await loop.close_mcp()  # drains the scheduled _archive_closed_session task

    assert "New session started" in result.content
    assert "/new archive failed for cli:test" in caplog.text

    closed_key = closed_record_key(KEY, last_active)
    closed_entry = read_session_summary_entry(loop.workspace, closed_key)
    assert closed_entry is not None
    assert "prior consolidated notes" in (closed_entry.body or closed_entry.summary or "")


@pytest.mark.asyncio
async def test_write_summary_failure_still_starts_new_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``write_session_summary()`` raising while filing the closed record
    must not stop ``/new`` from answering — only the failure is logged."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create(KEY)
    session.add_message("user", "hello")
    session.add_message("assistant", "hi")
    loop.sessions.save(session)

    loop.consolidator.archive = AsyncMock(
        return_value=("archived summary", {"entities": [], "topics": []})
    )
    monkeypatch.setattr(
        "durin.memory.session_summary_store.write_session_summary",
        MagicMock(side_effect=RuntimeError("disk full")),
    )

    with caplog.at_level(logging.ERROR, logger="durin.command.builtin"):
        result = await cmd_new(_ctx(loop))
        await loop.close_mcp()  # drains the scheduled _archive_closed_session task

    assert "New session started" in result.content
    assert "/new closed-conversation record failed for cli:test" in caplog.text
