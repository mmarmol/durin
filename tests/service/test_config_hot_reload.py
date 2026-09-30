import pytest

from durin.service.config import CONCURRENCY_CAP_KEYS, ConfigService
from durin.service.principal import Principal, Scope


def _principal():
    return Principal(
        subject="t",
        scopes=frozenset({Scope.CONFIG_READ.value, Scope.CONFIG_WRITE.value}),
        kind="local",
    )


def test_concurrency_cap_keys_are_the_three_caps():
    assert CONCURRENCY_CAP_KEYS == {
        "agents.defaults.max_concurrent_interactive",
        "agents.defaults.concurrency_ceiling",
        "agents.defaults.max_concurrent_subagents",
    }


@pytest.mark.asyncio
async def test_set_fires_callback_only_for_cap_keys(tmp_path, monkeypatch):
    calls = []
    svc = ConfigService(on_config_changed=lambda: calls.append(1))
    # Point config at a temp file with a valid minimal config.
    from durin.config.loader import get_config_path, load_config, save_config
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    save_config(load_config(), get_config_path())
    principal = _principal()

    from durin.service.config import ConfigSetCommand
    await svc.set(ConfigSetCommand(key="agents.defaults.concurrency_ceiling", value="16"), principal)
    assert calls == [1]  # cap key -> reload fired

    await svc.set(ConfigSetCommand(key="agents.defaults.max_messages", value="150"), principal)
    assert calls == [1]  # non-cap key -> no extra reload


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [
    'agents.defaults["concurrency_ceiling"]',
    '["agents"].defaults.concurrencyCeiling',
])
async def test_a_bracketed_cap_key_fires_the_callback_too(tmp_path, monkeypatch, key):
    calls = []
    svc = ConfigService(on_config_changed=lambda: calls.append(1))
    from durin.config.loader import get_config_path, load_config, save_config
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    save_config(load_config(), get_config_path())

    from durin.service.config import ConfigSetCommand
    await svc.set(ConfigSetCommand(key=key, value="16"), _principal())
    assert calls == [1]
