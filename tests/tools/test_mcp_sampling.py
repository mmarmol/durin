"""Tests for SP-6 server→client capabilities (roots, logging, sampling).

Phases 6a (roots) and 6b (logging) pure-unit tests — no transport needed.
"""
from __future__ import annotations

import pytest

from durin.agent.tools.mcp_connection import MCPServerConnection
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import MCPServerConfig


def _conn(workspace="/tmp/ws", **cfg_kw):
    return MCPServerConnection(
        "s", MCPServerConfig(**cfg_kw), ToolRegistry(), workspace=workspace
    )


# ---------------------------------------------------------------------------
# 6a — roots
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_roots_returns_workspace_file_uri(tmp_path):
    conn = _conn(workspace=str(tmp_path))
    cb = conn._make_list_roots_callback()
    result = await cb(context=None)
    assert len(result.roots) == 1
    assert str(result.roots[0].uri).startswith("file://")
    # resolve() on macOS expands /tmp → /private/tmp; check via Path comparison
    from pathlib import Path
    assert Path(result.roots[0].uri.path) == tmp_path.resolve()
    assert result.roots[0].name == "workspace"


@pytest.mark.asyncio
async def test_list_roots_empty_when_no_workspace():
    conn = MCPServerConnection("s", MCPServerConfig(), ToolRegistry(), workspace=None)
    cb = conn._make_list_roots_callback()
    result = await cb(context=None)
    assert result.roots == []


def test_session_kwargs_includes_roots_callback():
    conn = _conn()
    kwargs = conn._session_kwargs()
    assert "list_roots_callback" in kwargs
    assert callable(kwargs["list_roots_callback"])


# ---------------------------------------------------------------------------
# 6b — logging
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mcp_level,loguru_level", [
    ("debug", "DEBUG"), ("info", "INFO"), ("notice", "INFO"),
    ("warning", "WARNING"), ("error", "ERROR"),
    ("critical", "CRITICAL"), ("alert", "CRITICAL"), ("emergency", "CRITICAL"),
])
def test_level_mapping(mcp_level, loguru_level):
    from durin.agent.tools.mcp_sampling import mcp_log_level_to_loguru
    assert mcp_log_level_to_loguru(mcp_level) == loguru_level


@pytest.mark.asyncio
async def test_logging_callback_routes_to_logger():
    import mcp.types as types
    from durin.agent.tools.mcp_sampling import mcp_log_level_to_loguru  # noqa: F401

    logged = []

    conn = _conn()
    cb = conn._make_logging_callback()

    # Intercept loguru output via a sink
    from loguru import logger
    sink_id = logger.add(lambda msg: logged.append(msg), level="DEBUG", format="{message}")
    try:
        params = types.LoggingMessageNotificationParams(
            level="error", logger="weather-server", data="upstream API failed"
        )
        await cb(params=params)
    finally:
        logger.remove(sink_id)

    assert any(
        "weather-server" in str(m) and "upstream API failed" in str(m)
        for m in logged
    ), f"Expected log not found. Got: {logged}"


def test_session_kwargs_includes_logging_callback():
    conn = _conn()
    assert "logging_callback" in conn._session_kwargs()
