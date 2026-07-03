"""Title generation must survive reasoning models.

A model that thinks in-band (or gets truncated mid-reasoning) returns meta
text like "The user wants a concise title for the chat. The conversati…" —
that must never be persisted as the session title. Invalid output triggers
one retry, then a deterministic fallback from the user's first message; a
stored implausible auto-title self-heals on the next turn.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.session.manager import SessionManager
from durin.utils.webui_titles import (
    WEBUI_SESSION_METADATA_KEY,
    WEBUI_TITLE_METADATA_KEY,
    WEBUI_TITLE_USER_EDITED_METADATA_KEY,
    clean_generated_title,
    fallback_title_from_user_text,
    is_plausible_title,
    maybe_generate_webui_title,
)

LEAKED_REASONING = (
    "The user wants a concise title for the chat. The conversation is about "
    "building a dark paladin in D&D 5e, so I should mention"
)


def _make_session(sm: SessionManager, key: str = "websocket:t"):
    s = sm.get_or_create(key)
    s.metadata[WEBUI_SESSION_METADATA_KEY] = True
    s.add_message("user", "quiero armar un paladin dark en dnd 5e")
    s.add_message("assistant", "claro, veamos las opciones")
    sm.save(s)
    return s


def _provider_returning(*contents: str) -> MagicMock:
    provider = MagicMock()
    responses = []
    for content in contents:
        r = MagicMock()
        r.content = content
        responses.append(r)
    provider.chat_with_retry = AsyncMock(side_effect=responses)
    return provider


# ---------------------------------------------------------------------------
# is_plausible_title
# ---------------------------------------------------------------------------

def test_plausible_title_accepts_normal_titles():
    assert is_plausible_title("Dark paladin build for D&D 5e")
    assert is_plausible_title("Paladín oscuro en D&D")
    assert is_plausible_title("标题很短")  # CJK: no spaces, still one "word"


def test_plausible_title_rejects_leaked_reasoning():
    assert not is_plausible_title(LEAKED_REASONING)
    assert not is_plausible_title(
        "The user wants a concise title for the chat. The conversati…"
    )
    assert not is_plausible_title("El usuario quiere un título corto para el chat")
    assert not is_plausible_title("Let me think about what this chat covers")
    assert not is_plausible_title("")
    assert not is_plausible_title(None)


def test_plausible_title_rejects_prose_length_output():
    assert not is_plausible_title("word " * 40)


def test_plausible_title_accepts_title_after_think_block():
    assert is_plausible_title("<think>the user wants…</think>Dark paladin build")
    # Unclosed think block = the whole output is reasoning.
    assert not is_plausible_title("<think>the user wants a concise title")


def test_clean_strips_think_block():
    assert clean_generated_title("<think>hmm</think>Dark paladin build") == "Dark paladin build"


def test_fallback_title_from_user_text():
    assert fallback_title_from_user_text(
        "quiero armar un paladin dark en dnd 5e\ncon multiclase"
    ) == "quiero armar un paladin dark en dnd 5e"
    long = "x" * 200
    assert len(fallback_title_from_user_text(long)) <= 60


# ---------------------------------------------------------------------------
# maybe_generate_webui_title behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reasoning_output_retries_then_falls_back(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    _make_session(sm)
    provider = _provider_returning(LEAKED_REASONING, LEAKED_REASONING)

    generated = await maybe_generate_webui_title(
        sessions=sm, session_key="websocket:t", provider=provider, model="m",
    )

    assert generated is True
    assert provider.chat_with_retry.await_count == 2
    title = sm.get_or_create("websocket:t").metadata[WEBUI_TITLE_METADATA_KEY]
    assert title == "quiero armar un paladin dark en dnd 5e"


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    _make_session(sm)
    provider = _provider_returning(LEAKED_REASONING, "Paladín oscuro en D&D 5e")

    generated = await maybe_generate_webui_title(
        sessions=sm, session_key="websocket:t", provider=provider, model="m",
    )

    assert generated is True
    title = sm.get_or_create("websocket:t").metadata[WEBUI_TITLE_METADATA_KEY]
    assert title == "Paladín oscuro en D&D 5e"


@pytest.mark.asyncio
async def test_stored_reasoning_title_self_heals(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    s = _make_session(sm)
    s.metadata[WEBUI_TITLE_METADATA_KEY] = (
        "The user wants a concise title for the chat. The conversati…"
    )
    sm.save(s)
    provider = _provider_returning("Paladín oscuro en D&D 5e")

    generated = await maybe_generate_webui_title(
        sessions=sm, session_key="websocket:t", provider=provider, model="m",
    )

    assert generated is True
    title = sm.get_or_create("websocket:t").metadata[WEBUI_TITLE_METADATA_KEY]
    assert title == "Paladín oscuro en D&D 5e"


@pytest.mark.asyncio
async def test_stored_plausible_title_is_kept(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    s = _make_session(sm)
    s.metadata[WEBUI_TITLE_METADATA_KEY] = "Paladín oscuro en D&D 5e"
    sm.save(s)
    provider = _provider_returning("Anything Else")

    generated = await maybe_generate_webui_title(
        sessions=sm, session_key="websocket:t", provider=provider, model="m",
    )

    assert generated is False
    provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_edited_title_never_touched(tmp_path: Path) -> None:
    sm = SessionManager(tmp_path)
    s = _make_session(sm)
    # Even an implausible-looking name is sacred once the user set it.
    s.metadata[WEBUI_TITLE_METADATA_KEY] = "the user typed this weird name on purpose"
    s.metadata[WEBUI_TITLE_USER_EDITED_METADATA_KEY] = True
    sm.save(s)
    provider = _provider_returning("Anything Else")

    generated = await maybe_generate_webui_title(
        sessions=sm, session_key="websocket:t", provider=provider, model="m",
    )

    assert generated is False
    provider.chat_with_retry.assert_not_awaited()
