"""Editing a channel's config re-applies it to the running channel.

A live channel holds the config it was constructed with — the manager only
re-reads config in ``start_channel``. Without this, saving something like
``channels.slack.open_channels`` looked applied in the UI but changed
nothing until the next gateway restart, which is indistinguishable from the
setting not working.
"""

from __future__ import annotations

import pytest

from durin.service.config import ConfigService, ConfigSetCommand
from durin.service.principal import Principal, Scope


def _principal() -> Principal:
    return Principal(
        subject="t",
        scopes=frozenset({Scope.CONFIG_READ.value, Scope.CONFIG_WRITE.value}),
        kind="local",
    )


class FakeChannelManager:
    """Tracks running state the way ChannelManager does, so a stop actually
    stops and a start on a disabled channel raises."""

    def __init__(self, running: set[str] | None = None) -> None:
        self._running = set(running or ())
        self.calls: list[str] = []

    def get_status(self) -> dict[str, dict[str, bool]]:
        return {name: {"running": True} for name in self._running}

    async def stop_channel(self, name: str) -> None:
        self.calls.append(f"stop:{name}")
        self._running.discard(name)

    async def start_channel(self, name: str) -> None:
        self.calls.append(f"start:{name}")
        self._running.add(name)


@pytest.fixture
def svc_at(tmp_path, monkeypatch):
    """A ConfigService over an isolated DURIN_HOME with Slack enabled.

    A channel that is running is necessarily enabled on disk, so the seed
    mirrors that — otherwise the restart path would read ``enabled: false``
    and decline to start a channel it just stopped.
    """
    from durin.config.loader import get_config_path, load_config, save_config

    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    cfg = load_config()
    cfg.channels.slack = {"enabled": True, "bot_token": "${secret:SLACK_BOT_TOKEN}"}
    save_config(cfg, get_config_path())

    def build(manager: FakeChannelManager) -> ConfigService:
        return ConfigService(channel_manager=manager)

    return build


@pytest.mark.asyncio
async def test_channel_key_cycles_a_running_channel(svc_at) -> None:
    mgr = FakeChannelManager({"slack"})
    await svc_at(mgr).set(
        ConfigSetCommand(key="channels.slack.open_channels", value='["C0AKE2P92F7"]'),
        _principal(),
    )
    assert mgr.calls == ["stop:slack", "start:slack"]


@pytest.mark.asyncio
async def test_channel_key_leaves_a_stopped_channel_stopped(svc_at) -> None:
    """Saving config must not start a channel the user deliberately stopped."""
    mgr = FakeChannelManager()
    await svc_at(mgr).set(
        ConfigSetCommand(key="channels.slack.open_channels", value='["C0AKE2P92F7"]'),
        _principal(),
    )
    assert mgr.calls == []


@pytest.mark.asyncio
async def test_disabling_a_channel_stops_without_restarting(svc_at) -> None:
    mgr = FakeChannelManager({"slack"})
    await svc_at(mgr).set(
        ConfigSetCommand(key="channels.slack.enabled", value="false"), _principal()
    )
    assert mgr.calls == ["stop:slack"]


@pytest.mark.asyncio
async def test_global_channels_key_does_not_cycle(svc_at) -> None:
    """`channels.send_progress` is a global, not a per-channel section."""
    mgr = FakeChannelManager({"slack"})
    await svc_at(mgr).set(
        ConfigSetCommand(key="channels.send_progress", value="false"), _principal()
    )
    assert mgr.calls == []


@pytest.mark.asyncio
async def test_unrelated_key_does_not_cycle(svc_at) -> None:
    mgr = FakeChannelManager({"slack"})
    await svc_at(mgr).set(
        ConfigSetCommand(key="agents.defaults.max_messages", value="150"), _principal()
    )
    assert mgr.calls == []


@pytest.mark.asyncio
async def test_restart_failure_does_not_fail_the_save(svc_at) -> None:
    """The config is already on disk; a transport hiccup must not 500 the write."""

    class ExplodingManager(FakeChannelManager):
        async def start_channel(self, name: str) -> None:
            self.calls.append(f"start:{name}")
            raise RuntimeError("socket mode refused the handshake")

    mgr = ExplodingManager({"slack"})
    result = await svc_at(mgr).set(
        ConfigSetCommand(key="channels.slack.open_channels", value="[]"), _principal()
    )
    assert result.ok
    assert mgr.calls == ["stop:slack", "start:slack"]

    from durin.config.loader import get_config_path, load_config

    assert load_config(get_config_path()).channels.slack["open_channels"] == []
