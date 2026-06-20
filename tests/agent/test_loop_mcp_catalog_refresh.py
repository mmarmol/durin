from unittest.mock import MagicMock

import durin.agent.mcp_catalog_refresh as mcr
from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.config.schema import Config, ModelPresetConfig


class _FakeScheduler:
    instances: list = []

    def __init__(self, *a, **k):
        self.started = False
        _FakeScheduler.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        pass


def _loop(tmp_path, app_config) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=MagicMock(),
        workspace=tmp_path,
        model="base-model",
        context_window_tokens=1000,
        model_presets={"default": ModelPresetConfig(model="base-model")},
        app_config=app_config,
    )


def test_mcp_scheduler_started_when_enabled(tmp_path, monkeypatch):
    _FakeScheduler.instances = []
    monkeypatch.setattr(mcr, "McpCatalogRefreshScheduler", _FakeScheduler)
    cfg = Config()
    cfg.mcp_catalog_refresh.enabled = True
    loop = _loop(tmp_path, cfg)
    assert loop._mcp_catalog_refresh_scheduler is not None
    assert _FakeScheduler.instances and _FakeScheduler.instances[0].started is True


def test_mcp_scheduler_not_started_when_disabled(tmp_path, monkeypatch):
    _FakeScheduler.instances = []
    monkeypatch.setattr(mcr, "McpCatalogRefreshScheduler", _FakeScheduler)
    cfg = Config()
    cfg.mcp_catalog_refresh.enabled = False
    loop = _loop(tmp_path, cfg)
    assert loop._mcp_catalog_refresh_scheduler is None
    assert _FakeScheduler.instances == []


def test_mcp_scheduler_absent_without_app_config(tmp_path, monkeypatch):
    _FakeScheduler.instances = []
    monkeypatch.setattr(mcr, "McpCatalogRefreshScheduler", _FakeScheduler)
    loop = _loop(tmp_path, None)
    assert loop._mcp_catalog_refresh_scheduler is None
