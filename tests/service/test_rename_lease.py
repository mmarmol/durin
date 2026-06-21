"""Turn-lease serialization for HTTP session rename.

The rename handler (SessionsService.rename) writes session metadata and
calls save().  Without a turn lease it can clobber a concurrent agent turn
that is mid-write on the same session file.

Contract:
- Happy path (no contention): rename acquires the turn lease, reloads the
  session from disk, sets title + title_user_edited, saves, returns the
  updated title.
- Contention: lease held by an in-flight turn -> rename returns a ConflictError
  (busy/timeout) and DOES NOT apply any partial write.
- Reload semantics: rename reads fresh state from disk under the lease so it
  cannot overwrite turns that committed between the exists() check and the save.

See docs/architecture/concurrency.md.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import patch

import pytest

from durin.service.principal import Principal
from durin.service.sessions import SessionRenameCommand, SessionsService
from durin.service.types import ConflictError
from durin.session.manager import SessionManager
from durin.session.turn_lease import session_turn_lease
from durin.utils.webui_titles import (
    WEBUI_TITLE_METADATA_KEY,
    WEBUI_TITLE_USER_EDITED_METADATA_KEY,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sm(tmp_path: Path) -> SessionManager:
    mgr = SessionManager(tmp_path)
    from durin.session.manager import Session
    s = Session(key="websocket:alpha")
    s.add_message("user", "hello")
    mgr.save(s)
    return mgr


# ---------------------------------------------------------------------------
# Happy path: no contention
# ---------------------------------------------------------------------------


async def test_rename_happy_path_sets_title_and_saves(sm: SessionManager) -> None:
    """No contention: rename acquires lease, reloads, sets title, saves."""
    result = await SessionsService(sm).rename(
        SessionRenameCommand(key="websocket:alpha", title="My Chat"),
        Principal.local(),
    )
    assert result.title == "My Chat"
    # Confirm persisted.
    fresh = sm.reload("websocket:alpha")
    assert fresh.metadata.get(WEBUI_TITLE_METADATA_KEY) == "My Chat"
    assert fresh.metadata.get(WEBUI_TITLE_USER_EDITED_METADATA_KEY) is True


async def test_rename_happy_path_reloads_session_under_lease(sm: SessionManager) -> None:
    """rename must call sm.reload() inside the lease to read fresh disk state."""
    reload_calls: list[str] = []
    original_reload = sm.reload

    def spy_reload(key: str):
        reload_calls.append(key)
        return original_reload(key)

    sm.reload = spy_reload  # type: ignore[method-assign]

    await SessionsService(sm).rename(
        SessionRenameCommand(key="websocket:alpha", title="Reloaded"),
        Principal.local(),
    )
    assert "websocket:alpha" in reload_calls, "reload must be called inside the lease"


# ---------------------------------------------------------------------------
# Contention: lease held -> ConflictError, no partial write
# ---------------------------------------------------------------------------


async def test_rename_returns_conflict_when_lease_is_held(sm: SessionManager) -> None:
    """When the turn lease cannot be acquired, rename raises ConflictError."""

    @asynccontextmanager
    async def _busy_lease(path: Path, **kwargs) -> AsyncIterator[None]:
        raise TimeoutError("held by in-flight turn")
        yield  # pragma: no cover

    with patch("durin.service.sessions.session_turn_lease", _busy_lease):
        with pytest.raises(ConflictError):
            await SessionsService(sm).rename(
                SessionRenameCommand(key="websocket:alpha", title="Should Not Apply"),
                Principal.local(),
            )

    # Session must be unmodified.
    fresh = sm.reload("websocket:alpha")
    assert WEBUI_TITLE_METADATA_KEY not in fresh.metadata
    assert WEBUI_TITLE_USER_EDITED_METADATA_KEY not in fresh.metadata


async def test_rename_conflict_does_not_partial_write(sm: SessionManager) -> None:
    """On lease timeout, save() must never be called."""
    save_calls: list = []
    original_save = sm.save

    def spy_save(session, **kwargs):
        save_calls.append(session.key)
        return original_save(session, **kwargs)

    sm.save = spy_save  # type: ignore[method-assign]

    @asynccontextmanager
    async def _busy_lease(path: Path, **kwargs) -> AsyncIterator[None]:
        raise TimeoutError("held")
        yield  # pragma: no cover

    with patch("durin.service.sessions.session_turn_lease", _busy_lease):
        with pytest.raises(ConflictError):
            await SessionsService(sm).rename(
                SessionRenameCommand(key="websocket:alpha", title="Ghost"),
                Principal.local(),
            )

    assert save_calls == [], f"save must not be called on lease timeout; got {save_calls}"


# ---------------------------------------------------------------------------
# Reload-before-write: stale cached state is not clobbered
# ---------------------------------------------------------------------------


async def test_rename_sees_disk_state_not_stale_cache(sm: SessionManager, tmp_path: Path) -> None:
    """rename reloads from disk so it does not write a stale cached copy.

    Simulate: a concurrent turn appended a message between the service's
    initial sm.exists() check and the lease acquisition.  The reload under
    the lease must pick up that message, and the rename must preserve it.
    """
    key = "websocket:alpha"
    session_path = sm._get_session_path(key)

    # Spy on reload to inject an extra message AFTER the lease is acquired
    # (simulating what a concurrent turn would have written to disk).
    original_reload = sm.reload

    def inject_and_reload(k: str):
        # Append a message to disk directly to simulate a concurrent write.
        s = original_reload(k)
        s.add_message("assistant", "injected-by-concurrent-turn")
        sm.save(s)
        # Now reload again to return the injected state.
        return original_reload(k)

    sm.reload = inject_and_reload  # type: ignore[method-assign]

    result = await SessionsService(sm).rename(
        SessionRenameCommand(key=key, title="After Concurrent Turn"),
        Principal.local(),
    )
    assert result.title == "After Concurrent Turn"

    fresh = sm.reload(key)
    # The concurrent message must still be present.
    contents = [m["content"] for m in fresh.messages]
    assert "injected-by-concurrent-turn" in contents, (
        "rename clobbered a message written by a concurrent turn"
    )
    assert fresh.metadata.get(WEBUI_TITLE_METADATA_KEY) == "After Concurrent Turn"
