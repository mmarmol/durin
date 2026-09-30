"""Gateway startup imports and builds only the channels it will run.

Channel discovery imported every built-in channel module and loaded every
installed plugin just to find the enabled ones, so each boot paid for the SDK
imports of channels nobody had turned on (seconds on a small host, most of it
one SDK imported at module top).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

from durin.bus.events import OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.base import BaseChannel
from durin.channels.manager import ChannelManager
from durin.channels.registry import discover_channel_names
from durin.config.schema import Config

_EP_TARGET = "importlib.metadata.entry_points"


class _Plugin(BaseChannel):
    name = "fakeplugin"
    display_name = "Fake Plugin"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: OutboundMessage) -> None:
        pass


def _entry_point(name: str, cls: type, loaded: list[str]) -> SimpleNamespace:
    def load() -> type:
        loaded.append(name)
        return cls

    return SimpleNamespace(name=name, load=load)


def _forget_builtin_channel_modules(monkeypatch) -> None:
    """Drop every built-in channel module from ``sys.modules`` for this test,
    so an import during the test is visible as a fresh entry. monkeypatch puts
    the original modules back afterwards, in ``sys.modules`` and as attributes
    of the package, so a re-import here cannot leak into later tests."""
    import durin.channels as pkg

    for name in discover_channel_names():
        monkeypatch.delitem(sys.modules, f"durin.channels.{name}", raising=False)
        if hasattr(pkg, name):
            monkeypatch.setattr(pkg, name, getattr(pkg, name))


def _imported_builtin_channel_modules() -> list[str]:
    return [n for n in discover_channel_names() if f"durin.channels.{n}" in sys.modules]


def test_a_disabled_builtin_channel_module_is_not_imported(monkeypatch) -> None:
    _forget_builtin_channel_modules(monkeypatch)
    config = Config()
    config.channels.telegram = {"enabled": False, "token": "123:abc"}
    config.channels.fakeplugin = {"enabled": True, "allowFrom": ["*"]}
    loaded: list[str] = []

    with patch(_EP_TARGET, return_value=[_entry_point("fakeplugin", _Plugin, loaded)]):
        manager = ChannelManager(config, MessageBus())

    assert list(manager.channels) == ["fakeplugin"]
    assert _imported_builtin_channel_modules() == []


def test_an_enabled_builtin_channel_is_imported_and_built(monkeypatch) -> None:
    _forget_builtin_channel_modules(monkeypatch)
    config = Config()
    config.channels.email = {"enabled": True, "allowFrom": ["*"]}

    with patch(_EP_TARGET, return_value=[]):
        manager = ChannelManager(config, MessageBus())

    assert list(manager.channels) == ["email"]
    assert type(manager.channels["email"]).__module__ == "durin.channels.email"
    imported = _imported_builtin_channel_modules()
    assert "email" in imported
    # The email module may pull in its own helpers; no other channel's module.
    assert not set(imported) & {"discord", "slack", "telegram", "websocket", "whatsapp"}


def test_a_disabled_plugin_is_not_loaded() -> None:
    config = Config()
    config.channels.fakeplugin = {"enabled": False}
    loaded: list[str] = []

    with patch(_EP_TARGET, return_value=[_entry_point("fakeplugin", _Plugin, loaded)]):
        manager = ChannelManager(config, MessageBus())

    assert manager.channels == {}
    assert loaded == []

