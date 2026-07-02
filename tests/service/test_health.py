"""Unit tests for HealthService — mocks pip install + restart spawn."""

from __future__ import annotations

import pytest

from durin.service.health import (
    ExtrasEnsureCommand,
    ExtrasRestartCommand,
    ExtrasStatusQuery,
    HealthService,
    LogsListQuery,
)
from durin.service.principal import Principal
from durin.service.types import UnavailableError, ValidationFailedError


@pytest.fixture()
def svc():
    return HealthService()


@pytest.fixture()
def local():
    return Principal.local()


# ---------------------------------------------------------------------------
# extras_status
# ---------------------------------------------------------------------------


async def test_extras_status_known_present(svc, local, monkeypatch):
    import durin.extras as ex

    monkeypatch.setattr(ex, "_module_present", lambda _: True)
    result = await svc.extras_status(ExtrasStatusQuery(feature="web_search"), local)
    assert result.present is True
    assert result.extra == "web"
    assert result.needs_restart is False
    assert result.label == "Web search"


async def test_extras_status_known_absent(svc, local, monkeypatch):
    import durin.extras as ex

    monkeypatch.setattr(ex, "_module_present", lambda _: False)
    result = await svc.extras_status(ExtrasStatusQuery(feature="cross_encoder"), local)
    assert result.present is False
    assert result.needs_restart is True


async def test_extras_status_unknown_raises(svc, local):
    with pytest.raises(ValidationFailedError) as excinfo:
        await svc.extras_status(ExtrasStatusQuery(feature="nope"), local)
    assert "unknown feature" in excinfo.value.message


# ---------------------------------------------------------------------------
# extras_ensure
# ---------------------------------------------------------------------------


async def test_extras_ensure_installed_with_restart(svc, local, monkeypatch):
    import durin.extras as ex

    monkeypatch.setattr(
        ex,
        "ensure_extra",
        lambda feature, *, config: ex.EnsureResult("installed", feature, True, ""),
    )
    monkeypatch.setattr("durin.config.loader.load_config", lambda: object())
    result = await svc.extras_ensure(
        ExtrasEnsureCommand(feature="cross_encoder", restart=True), local
    )
    assert result.status == "installed"
    assert result.needs_restart is True
    assert result.restarting is True


async def test_extras_ensure_installed_no_restart_flag(svc, local, monkeypatch):
    import durin.extras as ex

    monkeypatch.setattr(
        ex,
        "ensure_extra",
        lambda feature, *, config: ex.EnsureResult("installed", feature, True, ""),
    )
    monkeypatch.setattr("durin.config.loader.load_config", lambda: object())
    result = await svc.extras_ensure(
        ExtrasEnsureCommand(feature="cross_encoder", restart=False), local
    )
    assert result.status == "installed"
    assert result.restarting is False


async def test_extras_ensure_already_present(svc, local, monkeypatch):
    import durin.extras as ex

    monkeypatch.setattr(
        ex,
        "ensure_extra",
        lambda feature, *, config: ex.EnsureResult("present", feature, False, ""),
    )
    monkeypatch.setattr("durin.config.loader.load_config", lambda: object())
    result = await svc.extras_ensure(
        ExtrasEnsureCommand(feature="web_search", restart=True), local
    )
    assert result.status == "present"
    assert result.restarting is False


async def test_extras_ensure_unknown_feature_raises(svc, local, monkeypatch):
    monkeypatch.setattr("durin.config.loader.load_config", lambda: object())
    with pytest.raises(ValidationFailedError):
        await svc.extras_ensure(ExtrasEnsureCommand(feature="nope"), local)


# ---------------------------------------------------------------------------
# extras_restart
# ---------------------------------------------------------------------------


async def test_extras_restart_returns_restarting(svc, local):
    result = await svc.extras_restart(ExtrasRestartCommand(), local)
    assert result.restarting is True


# ---------------------------------------------------------------------------
# logs_list
# ---------------------------------------------------------------------------


async def test_logs_list_gateway_ok(svc, local, monkeypatch, tmp_path):
    from durin.logs.reader import LogPage

    fake_page = LogPage(
        lines=[{"msg": "hello"}],
        next_cursor=None,
        scanned_through_ts=None,
        has_more=False,
    )
    fake_facets: dict = {}
    monkeypatch.setattr("durin.logs.reader.read_page", lambda d, q: fake_page)
    monkeypatch.setattr("durin.logs.reader.compute_facets", lambda d, s: fake_facets)
    monkeypatch.setattr(
        "durin.cli.gateway_daemon.daemon_logs_path",
        lambda: tmp_path / "gateway.log",
    )
    result = await svc.logs_list(LogsListQuery(source="gateway"), local)
    assert result.lines == [{"msg": "hello"}]
    assert result.has_more is False


async def test_logs_list_read_error_raises_unavailable(svc, local, monkeypatch, tmp_path):
    def _raise(d, q):
        raise RuntimeError("disk gone")

    monkeypatch.setattr("durin.logs.reader.read_page", _raise)
    monkeypatch.setattr("durin.logs.reader.compute_facets", lambda d, s: {})
    monkeypatch.setattr(
        "durin.cli.gateway_daemon.daemon_logs_path",
        lambda: tmp_path / "gateway.log",
    )
    with pytest.raises(UnavailableError) as excinfo:
        await svc.logs_list(LogsListQuery(), local)
    assert "log read failed" in excinfo.value.message


# ---------------------------------------------------------------------------
# status (runtime snapshot)
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self, running: bool) -> None:
        self.is_running = running


class _FakeChannelManager:
    def __init__(self, channels: dict) -> None:
        self.channels = channels


class _FakeCron:
    def status(self) -> dict:
        return {"enabled": True, "jobs": 3, "next_wake_at_ms": 1_700_000_000_000}


async def test_status_reports_version_channels_and_cron(local, monkeypatch, tmp_path):
    from durin import __version__
    from durin.config.schema import Config
    from durin.service.health import RuntimeStatusQuery

    cfg = Config()
    cfg.channels.__pydantic_extra__["telegram"] = {"enabled": True}
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)

    svc = HealthService(
        channel_manager=_FakeChannelManager({"telegram": _FakeChannel(True)}),
        cron_service=_FakeCron(),
    )
    result = await svc.status(RuntimeStatusQuery(), local)
    assert result.version == __version__
    assert result.cron == {"enabled": True, "jobs": 3, "next_wake_at_ms": 1_700_000_000_000}
    assert result.channels == [{"name": "telegram", "enabled": True, "running": True}]


async def test_status_without_deps_degrades_to_config_only(local, monkeypatch):
    from durin.config.schema import Config
    from durin.service.health import RuntimeStatusQuery

    cfg = Config()
    cfg.channels.__pydantic_extra__["telegram"] = {"enabled": True}
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)

    svc = HealthService()
    result = await svc.status(RuntimeStatusQuery(), local)
    assert result.cron is None
    # Enabled in config but no live manager → running=False (nothing runs here).
    assert result.channels == [{"name": "telegram", "enabled": True, "running": False}]


async def test_status_skips_disabled_and_not_running_channels(local, monkeypatch):
    from durin.config.schema import Config
    from durin.service.health import RuntimeStatusQuery

    cfg = Config()
    cfg.channels.__pydantic_extra__["telegram"] = {"enabled": False}
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)

    svc = HealthService(channel_manager=_FakeChannelManager({}))
    result = await svc.status(RuntimeStatusQuery(), local)
    assert result.channels == []
