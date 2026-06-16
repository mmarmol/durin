"""Tests for MCPServerConfig — the server-level ``enabled`` flag."""
from __future__ import annotations

from pathlib import Path

from durin.config.loader import load_config, save_config
from durin.config.schema import Config, MCPServerConfig


def test_mcp_server_enabled_defaults_true() -> None:
    assert MCPServerConfig().enabled is True


def test_mcp_server_enabled_false_round_trips(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    config = Config()
    config.tools.mcp_servers["probe"] = MCPServerConfig(
        url="https://example.com/mcp", enabled=False
    )
    save_config(config, cfg)

    loaded = load_config(cfg)
    assert "probe" in loaded.tools.mcp_servers
    assert loaded.tools.mcp_servers["probe"].enabled is False


def test_mcp_server_enabled_omitted_from_dump_when_default() -> None:
    # exclude_defaults keeps the persisted config noise-free: enabled only
    # appears on disk when the user turns a server off.
    on = MCPServerConfig(command="npx").model_dump(by_alias=True, exclude_defaults=True)
    assert "enabled" not in on
    off = MCPServerConfig(command="npx", enabled=False).model_dump(
        by_alias=True, exclude_defaults=True
    )
    assert off["enabled"] is False
