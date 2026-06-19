"""C-Task 4 — McpCatalogRefreshConfig defaults and wiring on Config."""
from durin.config.schema import Config, McpCatalogRefreshConfig

_EXPECTED_URL = "https://github.com/mmarmol/durin/releases/download/catalog/mcp_catalog.json"


def test_defaults():
    cfg = McpCatalogRefreshConfig()
    assert cfg.enabled is True
    assert cfg.url == _EXPECTED_URL
    assert cfg.interval_hours == 168


def test_interval_hours_camel_alias():
    cfg = McpCatalogRefreshConfig.model_validate({"intervalHours": 24})
    assert cfg.interval_hours == 24


def test_reachable_on_config():
    cfg = Config()
    assert isinstance(cfg.mcp_catalog_refresh, McpCatalogRefreshConfig)
    assert cfg.mcp_catalog_refresh.enabled is True
    assert cfg.mcp_catalog_refresh.interval_hours == 168


def test_config_camel_alias():
    cfg = Config.model_validate({"mcpCatalogRefresh": {"intervalHours": 72, "enabled": False}})
    assert cfg.mcp_catalog_refresh.enabled is False
    assert cfg.mcp_catalog_refresh.interval_hours == 72


def test_config_snake_alias():
    cfg = Config.model_validate({"mcp_catalog_refresh": {"interval_hours": 48}})
    assert cfg.mcp_catalog_refresh.interval_hours == 48
