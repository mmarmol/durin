"""SP1: SessionsService — unit tests (called directly, no HTTP)."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.service.principal import Principal, Scope
from durin.service.sessions import (
    SessionDeleteCommand,
    SessionMessagesQuery,
    SessionRenameCommand,
    SessionsListQuery,
    SessionsService,
    WebuiThreadQuery,
)
from durin.service.types import (
    ForbiddenError,
    NotFoundError,
    UnavailableError,
    ValidationFailedError,
)


@pytest.fixture()
def sm(tmp_path: Path):
    """Real SessionManager seeded with a couple of sessions."""
    from durin.session.manager import Session, SessionManager

    mgr = SessionManager(tmp_path)
    for key in ("websocket:alpha", "websocket:beta", "cli:direct"):
        s = Session(key=key)
        s.add_message("user", f"hi from {key}")
        mgr.save(s)
    return mgr


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_returns_all_channel_sessions(sm) -> None:
    result = await SessionsService(sm).list(SessionsListQuery(), Principal.local())
    keys = {s["key"] for s in result.sessions}
    assert keys == {"websocket:alpha", "websocket:beta", "cli:direct"}


async def test_list_includes_channel_field(sm) -> None:
    result = await SessionsService(sm).list(SessionsListQuery(), Principal.local())
    channels = {s["key"]: s["channel"] for s in result.sessions}
    assert channels["websocket:alpha"] == "websocket"
    assert channels["websocket:beta"] == "websocket"
    assert channels["cli:direct"] == "cli"


async def test_list_strips_path_field(sm) -> None:
    result = await SessionsService(sm).list(SessionsListQuery(), Principal.local())
    assert all("path" not in s for s in result.sessions)


async def test_list_raises_unavailable_when_no_manager() -> None:
    with pytest.raises(UnavailableError):
        await SessionsService(None).list(SessionsListQuery(), Principal.local())


async def test_list_requires_read_scope(sm) -> None:
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await SessionsService(sm).list(SessionsListQuery(), principal)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


async def test_messages_returns_session_data(sm) -> None:
    result = await SessionsService(sm).messages(
        SessionMessagesQuery(key="websocket:alpha"), Principal.local()
    )
    assert result.data["key"] == "websocket:alpha"
    assert isinstance(result.data["messages"], list)
    assert result.data["messages"][0]["role"] == "user"


async def test_messages_raises_not_found_for_missing_session(sm) -> None:
    with pytest.raises(NotFoundError):
        await SessionsService(sm).messages(
            SessionMessagesQuery(key="websocket:ghost"), Principal.local()
        )


async def test_messages_returns_data_for_non_websocket_key(sm) -> None:
    result = await SessionsService(sm).messages(
        SessionMessagesQuery(key="cli:direct"), Principal.local()
    )
    assert result.data["key"] == "cli:direct"


async def test_messages_raises_unavailable_when_no_manager() -> None:
    with pytest.raises(UnavailableError):
        await SessionsService(None).messages(
            SessionMessagesQuery(key="websocket:x"), Principal.local()
        )


async def test_messages_requires_read_scope(sm) -> None:
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await SessionsService(sm).messages(
            SessionMessagesQuery(key="websocket:alpha"), principal
        )


# ---------------------------------------------------------------------------
# webui_thread — key validation only (build happens in the shim)
# ---------------------------------------------------------------------------


async def test_webui_thread_validates_any_key() -> None:
    for key in ("websocket:abc", "telegram:123", "cli:direct"):
        result = await SessionsService(None).webui_thread(
            WebuiThreadQuery(key=key), Principal.local()
        )
        # Sentinel result — shim discards this and calls build_webui_thread_response.
        assert result.data == {}


async def test_webui_thread_requires_read_scope() -> None:
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await SessionsService(None).webui_thread(
            WebuiThreadQuery(key="websocket:abc"), principal
        )


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_removes_session(sm, tmp_path: Path) -> None:
    path = sm._get_session_path("websocket:alpha")
    assert path.exists()
    result = await SessionsService(sm).delete(
        SessionDeleteCommand(key="websocket:alpha"), Principal.local()
    )
    assert result.deleted is True
    assert not path.exists()


async def test_delete_removes_non_websocket_session(sm) -> None:
    result = await SessionsService(sm).delete(
        SessionDeleteCommand(key="cli:direct"), Principal.local()
    )
    assert result.deleted is True


async def test_delete_returns_false_for_nonexistent_session(sm) -> None:
    result = await SessionsService(sm).delete(
        SessionDeleteCommand(key="websocket:ghost"), Principal.local()
    )
    assert result.deleted is False


async def test_delete_raises_unavailable_when_no_manager() -> None:
    with pytest.raises(UnavailableError):
        await SessionsService(None).delete(
            SessionDeleteCommand(key="websocket:x"), Principal.local()
        )


async def test_delete_requires_write_scope(sm) -> None:
    principal = Principal.remote("t", frozenset({Scope.SESSIONS_READ.value}))
    with pytest.raises(ForbiddenError):
        await SessionsService(sm).delete(
            SessionDeleteCommand(key="websocket:alpha"), principal
        )


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


async def test_rename_sets_title(sm) -> None:
    result = await SessionsService(sm).rename(
        SessionRenameCommand(key="websocket:alpha", title="My Chat"), Principal.local()
    )
    assert result.title == "My Chat"
    # Title is persisted in the session metadata.
    from durin.utils.webui_titles import WEBUI_TITLE_METADATA_KEY

    session = sm.get_or_create("websocket:alpha")
    assert session.metadata.get(WEBUI_TITLE_METADATA_KEY) == "My Chat"


async def test_rename_raises_not_found_for_missing_session(sm) -> None:
    with pytest.raises(NotFoundError):
        await SessionsService(sm).rename(
            SessionRenameCommand(key="websocket:ghost", title="X"), Principal.local()
        )


async def test_rename_sets_title_for_non_websocket_key(sm) -> None:
    result = await SessionsService(sm).rename(
        SessionRenameCommand(key="cli:direct", title="Terminal Session"), Principal.local()
    )
    assert result.title == "Terminal Session"


async def test_rename_raises_validation_for_empty_title(sm) -> None:
    with pytest.raises(ValidationFailedError):
        await SessionsService(sm).rename(
            SessionRenameCommand(key="websocket:alpha", title=""), Principal.local()
        )


async def test_rename_raises_unavailable_when_no_manager() -> None:
    with pytest.raises(UnavailableError):
        await SessionsService(None).rename(
            SessionRenameCommand(key="websocket:x", title="Hi"), Principal.local()
        )


async def test_rename_requires_write_scope(sm) -> None:
    principal = Principal.remote("t", frozenset({Scope.SESSIONS_READ.value}))
    with pytest.raises(ForbiddenError):
        await SessionsService(sm).rename(
            SessionRenameCommand(key="websocket:alpha", title="X"), principal
        )


async def test_rename_truncates_long_title(sm) -> None:
    from durin.utils.webui_titles import TITLE_MAX_CHARS

    long_title = "x" * (TITLE_MAX_CHARS + 50)
    result = await SessionsService(sm).rename(
        SessionRenameCommand(key="websocket:alpha", title=long_title), Principal.local()
    )
    assert len(result.title) <= TITLE_MAX_CHARS
    assert result.title.endswith("…")
