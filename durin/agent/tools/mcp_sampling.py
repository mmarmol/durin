"""MCP server→client capability helpers (SP-6).

Phases 6a/6b: roots + logging utilities consumed by MCPServerConnection.
"""
from __future__ import annotations

_MCP_TO_LOGURU: dict[str, str] = {
    "debug": "DEBUG",
    "info": "INFO",
    "notice": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
    "alert": "CRITICAL",
    "emergency": "CRITICAL",
}


def mcp_log_level_to_loguru(level: str) -> str:
    """Map an RFC-5424 MCP logging level to a loguru level name."""
    return _MCP_TO_LOGURU.get(level, "INFO")
