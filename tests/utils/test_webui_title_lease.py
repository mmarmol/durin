"""Turn-lease serialization for webui background title generation.

maybe_generate_webui_title previously ran the LLM call AND the session save
in the same call, with no turn lease, so its save() could clobber a
concurrent agent turn.

Contract:
- The LLM call runs OUTSIDE the lease (long; must not hold the lock).
- The save happens INSIDE the lease.
- Under the lease, the session is reloaded from disk so the save is based on
  fresh state.
- If title_user_edited was set between the LLM call and the lease acquisition,
  the generated title is discarded (guard still applies on reload).
- A concurrent turn's message append is not overwritten.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.session.manager import Session, SessionManager
from durin.session.turn_lease import session_turn_lease
from durin.utils.webui_titles import (
    WEBUI_SESSION_METADATA_KEY,
    WEBUI_TITLE_METADATA_KEY,
    WEBUI_TITLE_USER_EDITED_METADATA_KEY,
    maybe_generate_webui_title,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_webui_session(sm: SessionManager, key: str = "websocket:t") -> Session:
    s = sm.get_or_create(key)
    s.metadata[WEBUI_SESSION_METADATA_KEY] = True
    s.add_message("user", "tell me a joke")
    s.add_message("assistant", "why did the chicken cross the road")
    sm.save(s)
    return s


def _make_provider(title: str = "Generated Title") -> MagicMock:
    provider = MagicMock()
    response = MagicMock()
    response.content = title
    provider.chat_with_retry = AsyncMock(return_value=response)
    return provider


# ---------------------------------------------------------------------------
# Test 1: LLM call is invoked OUTSIDE the lease
# ---------------------------------------------------------------------------


async def test_llm_call_happens_outside_the_lease(tmp_path: Path) -> None:
    """The LLM title generation must run before the lease is acquired,
    and the save must run INSIDE the lease.

    Verified by probing the turn lock file at LLM call time (must be free)
    and at save time (must be held).  A bug that moves the LLM call inside
    the lease makes lease_held_during_llm == [True].  A bug that does the
    save outside the lease makes lease_held_during_save == [False].
    """
    key = "websocket:outside"
    sm = SessionManager(tmp_path)
    _make_webui_session(sm, key)

    session_path = sm._get_session_path(key)
    turn_lock_path = session_path.with_suffix(".turn.lock")
    lock_file_path = Path(f"{turn_lock_path}.lock")

    lease_held_during_llm: list[bool] = []
    lease_held_during_save: list[bool] = []

    import fcntl

    def _probe_lease_free() -> bool:
        """Return True if the turn lock IS held (cannot acquire NB flock)."""
        try:
            fp = lock_file_path.open("a+", encoding="utf-8")
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            fp.close()
            return False  # lock was free
        except OSError:
            return True  # lock was held

    async def spy_chat_with_retry(*args, **kwargs):
        lease_held_during_llm.append(_probe_lease_free())
        response = MagicMock()
        response.content = "Good Title"
        return response

    original_save = sm.save

    def spy_save(session, **kwargs):
        lease_held_during_save.append(_probe_lease_free())
        return original_save(session, **kwargs)

    sm.save = spy_save  # type: ignore[method-assign]

    provider = MagicMock()
    provider.chat_with_retry = spy_chat_with_retry

    result = await maybe_generate_webui_title(
        sessions=sm,
        session_key=key,
        provider=provider,
        model="test-model",
    )

    assert result is True
    assert lease_held_during_llm == [False], (
        "The turn lease must NOT be held during the LLM call; "
        f"got lease_held={lease_held_during_llm}"
    )
    assert lease_held_during_save, "save must be called at least once"
    assert all(lease_held_during_save), (
        "The turn lease MUST be held during save(); "
        f"got lease_held={lease_held_during_save}"
    )


# ---------------------------------------------------------------------------
# Test 2: save happens inside the lease + session is reloaded
# ---------------------------------------------------------------------------


async def test_save_happens_inside_the_lease(tmp_path: Path) -> None:
    """The session save must occur while the turn lease is held."""
    key = "websocket:inside"
    sm = SessionManager(tmp_path)
    _make_webui_session(sm, key)

    session_path = sm._get_session_path(key)
    save_calls: list[bool] = []  # True = lease held at save time
    original_save = sm.save

    lease_acquisition_count = [0]
    original_lease = session_turn_lease

    @asynccontextmanager
    async def spy_lease(path: Path, **kwargs) -> AsyncIterator[None]:
        lease_acquisition_count[0] += 1
        async with original_lease(path, **kwargs):
            yield

    def spy_save(session, **kwargs):
        # If the lease is held, the turn lock file's flock is taken.
        # We approximate by checking lease_acquisition_count > 0 and the
        # context is inside the spy_lease.  A simpler proxy: check that
        # save is called after at least one lease was entered.
        save_calls.append(lease_acquisition_count[0] > 0)
        return original_save(session, **kwargs)

    sm.save = spy_save  # type: ignore[method-assign]

    with patch("durin.utils.webui_titles.session_turn_lease", spy_lease):
        result = await maybe_generate_webui_title(
            sessions=sm,
            session_key=key,
            provider=_make_provider("Saved Under Lease"),
            model="test-model",
        )

    assert result is True
    assert save_calls, "save must be called"
    assert all(save_calls), "save must happen while at least one lease was acquired"
    assert lease_acquisition_count[0] >= 1, "lease must be acquired for the save"


async def test_save_uses_reloaded_session(tmp_path: Path) -> None:
    """Under the lease, the session is reloaded before the title is set."""
    key = "websocket:reload"
    sm = SessionManager(tmp_path)
    _make_webui_session(sm, key)

    reload_calls: list[str] = []
    original_reload = sm.reload

    def spy_reload(k: str):
        reload_calls.append(k)
        return original_reload(k)

    sm.reload = spy_reload  # type: ignore[method-assign]

    original_lease = session_turn_lease

    @asynccontextmanager
    async def transparent_lease(path: Path, **kwargs) -> AsyncIterator[None]:
        async with original_lease(path, **kwargs):
            yield

    with patch("durin.utils.webui_titles.session_turn_lease", transparent_lease):
        await maybe_generate_webui_title(
            sessions=sm,
            session_key=key,
            provider=_make_provider("After Reload"),
            model="test-model",
        )

    assert key in reload_calls, "session must be reloaded under the lease before save"


# ---------------------------------------------------------------------------
# Test 3: title_user_edited guard is re-checked on reload
# ---------------------------------------------------------------------------


async def test_title_not_set_when_user_edited_on_reload(tmp_path: Path) -> None:
    """If title_user_edited becomes True between the LLM call and the save,
    the generated title must be discarded (guard re-checked on reloaded state).
    """
    key = "websocket:guard"
    sm = SessionManager(tmp_path)
    _make_webui_session(sm, key)

    original_reload = sm.reload

    def inject_user_edit_then_reload(k: str):
        # Simulate: user renamed the session between LLM call and our save.
        s = original_reload(k)
        s.metadata[WEBUI_TITLE_USER_EDITED_METADATA_KEY] = True
        s.metadata[WEBUI_TITLE_METADATA_KEY] = "User Chosen Title"
        sm.save(s)
        return original_reload(k)

    sm.reload = inject_user_edit_then_reload  # type: ignore[method-assign]

    original_lease = session_turn_lease

    @asynccontextmanager
    async def transparent_lease(path: Path, **kwargs) -> AsyncIterator[None]:
        async with original_lease(path, **kwargs):
            yield

    with patch("durin.utils.webui_titles.session_turn_lease", transparent_lease):
        result = await maybe_generate_webui_title(
            sessions=sm,
            session_key=key,
            provider=_make_provider("Discarded LLM Title"),
            model="test-model",
        )

    # Should return False (did not set title) because the guard fired on reload.
    assert result is False, "title generation must be a no-op when user_edited is set on reload"

    fresh = sm.reload(key)
    assert fresh.metadata.get(WEBUI_TITLE_METADATA_KEY) == "User Chosen Title", (
        "user-chosen title must not be overwritten by the LLM-generated one"
    )


# ---------------------------------------------------------------------------
# Test 4: concurrent turn message is not clobbered by title save
# ---------------------------------------------------------------------------


async def test_title_save_preserves_concurrent_turn_messages(tmp_path: Path) -> None:
    """A message appended by a concurrent turn must survive the title save.

    The title generator reloads under the lease, so any messages committed
    to disk before the lease was acquired are visible in the reloaded session.
    The save must preserve them.

    The final verification intentionally uses a FRESH SessionManager (not the
    spy-patched one) so that the injection spy cannot re-add the follow-up
    message during the read — a maximal clobber (writing an empty session)
    would make this assertion fail.
    """
    key = "websocket:preserve"
    sm = SessionManager(tmp_path)
    _make_webui_session(sm, key)

    original_reload = sm.reload

    def append_turn_then_reload(k: str):
        # Simulate a concurrent turn that committed while LLM was running.
        s = original_reload(k)
        s.add_message("user", "follow-up from concurrent turn")
        sm.save(s)
        return original_reload(k)

    sm.reload = append_turn_then_reload  # type: ignore[method-assign]

    original_lease = session_turn_lease

    @asynccontextmanager
    async def transparent_lease(path: Path, **kwargs) -> AsyncIterator[None]:
        async with original_lease(path, **kwargs):
            yield

    with patch("durin.utils.webui_titles.session_turn_lease", transparent_lease):
        result = await maybe_generate_webui_title(
            sessions=sm,
            session_key=key,
            provider=_make_provider("Title After Turn"),
            model="test-model",
        )

    # Use a fresh SessionManager so the injection spy cannot re-inject the
    # follow-up message during verification.  A real clobber (e.g. an impl
    # that saves a brand-new empty session) will make these assertions fail.
    fresh_sm = SessionManager(tmp_path)
    fresh = fresh_sm.get_or_create(key)
    contents = [m["content"] for m in fresh.messages]
    assert "follow-up from concurrent turn" in contents, (
        "title save must not clobber a message written by a concurrent turn"
    )
    assert fresh.metadata.get(WEBUI_TITLE_METADATA_KEY) == "Title After Turn"
