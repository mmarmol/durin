import types

import durin.channels.manager as mgr
import durin.extras as ex


def _manager(slack_enabled: bool, discord_enabled: bool):
    m = mgr.ChannelManager.__new__(mgr.ChannelManager)
    m.config = types.SimpleNamespace(
        channels=types.SimpleNamespace(
            slack=types.SimpleNamespace(enabled=slack_enabled),
            discord=types.SimpleNamespace(enabled=discord_enabled),
        )
    )
    return m


def test_ensure_channel_extras_installs_missing(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    calls = []
    monkeypatch.setattr(ex, "ensure_or_note", lambda feature, *, config: calls.append(feature))
    _manager(slack_enabled=True, discord_enabled=False)._ensure_channel_extras()
    assert calls == ["slack"]  # only the enabled+missing one


def test_ensure_channel_extras_skips_when_present(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: True)
    calls = []
    monkeypatch.setattr(ex, "ensure_or_note", lambda feature, *, config: calls.append(feature))
    _manager(slack_enabled=True, discord_enabled=True)._ensure_channel_extras()
    assert calls == []  # deps present → no install


def test_ensure_channel_extras_both_missing(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    calls = []
    monkeypatch.setattr(ex, "ensure_or_note", lambda feature, *, config: calls.append(feature))
    _manager(slack_enabled=True, discord_enabled=True)._ensure_channel_extras()
    assert calls == ["slack", "discord"]


def test_matrix_registered_as_feature_extra():
    from durin.extras import REGISTRY

    fe = REGISTRY["matrix"]
    assert fe.extra == "matrix"
    assert fe.module == "nio"
    assert fe.needs_restart is True


def test_ensure_channel_extras_covers_matrix(monkeypatch):
    """The ensure loop derives from the extras REGISTRY, not a hardcoded tuple."""
    calls = []
    monkeypatch.setattr(ex, "ensure_or_note", lambda feature, *, config: calls.append(feature))
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    m = mgr.ChannelManager.__new__(mgr.ChannelManager)
    m.config = types.SimpleNamespace(
        channels=types.SimpleNamespace(
            matrix=types.SimpleNamespace(enabled=True),
        )
    )
    m._ensure_channel_extras()
    assert "matrix" in calls
