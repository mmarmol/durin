"""Tests for plaintext-key migration — migrate_plaintext_provider_keys."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from durin.security.secrets import (
    SecretStore,
    is_secret_ref,
    migrate_plaintext_provider_keys,
    resolve_secret,
)


@pytest.fixture
def config_at(tmp_path):
    """Patch get_config_path so the store lands next to a temp config."""
    config_path = tmp_path / "config.json"
    with patch("durin.config.loader.get_config_path", return_value=config_path):
        yield config_path


def _write_split(config_path, providers: dict) -> None:
    """Lay down a split-layout config with the given providers section."""
    split = config_path.with_suffix(config_path.suffix + ".d")
    split.mkdir(parents=True, exist_ok=True)
    (split / "providers.json").write_text(json.dumps(providers), encoding="utf-8")
    config_path.write_text(json.dumps({"_layout": "split"}), encoding="utf-8")


def test_migrate_split_layout_moves_key_to_store(config_at) -> None:
    _write_split(config_at, {"zhipu": {"apiKey": "sk-zhipu-real"}})

    created = migrate_plaintext_provider_keys(config_at)
    assert created == ["ZHIPU_API_KEY"]

    # Config now holds a reference, not the plaintext.
    providers = json.loads(
        (config_at.with_suffix(".json.d") / "providers.json").read_text()
    )
    assert is_secret_ref(providers["zhipu"]["apiKey"])
    assert "sk-zhipu-real" not in json.dumps(providers)

    # The store holds the value, classified.
    entry = SecretStore(path=config_at.parent / "secrets.json").load().get(
        "ZHIPU_API_KEY"
    )
    assert entry is not None
    assert entry.value == "sk-zhipu-real"
    assert entry.service == "provider:zhipu"
    assert entry.scope == ["provider:zhipu"]
    assert entry.origin == "migration"


def test_migrate_monolith_layout(config_at) -> None:
    config_at.write_text(
        json.dumps({"providers": {"openai": {"apiKey": "sk-openai"}}}),
        encoding="utf-8",
    )
    created = migrate_plaintext_provider_keys(config_at)
    assert created == ["OPENAI_API_KEY"]
    mono = json.loads(config_at.read_text())
    assert is_secret_ref(mono["providers"]["openai"]["apiKey"])


def test_migrate_is_idempotent(config_at) -> None:
    _write_split(config_at, {"zhipu": {"apiKey": "sk-zhipu"}})
    assert migrate_plaintext_provider_keys(config_at) == ["ZHIPU_API_KEY"]
    # Second run: everything is already a reference → no-op.
    assert migrate_plaintext_provider_keys(config_at) == []


def test_migrate_skips_already_referenced(config_at) -> None:
    _write_split(config_at, {"zhipu": {"apiKey": "${secret:ZHIPU_API_KEY}"}})
    assert migrate_plaintext_provider_keys(config_at) == []


def test_migrate_backs_up_config(config_at) -> None:
    _write_split(config_at, {"zhipu": {"apiKey": "sk-zhipu"}})
    migrate_plaintext_provider_keys(config_at)
    # backup_config snapshots the split dir as <name>.bak.<stamp>.
    backups = list(config_at.parent.glob("config.json.d.bak.*"))
    assert backups, "expected a config backup directory"


def test_resolution_round_trip_after_migration(config_at) -> None:
    """End to end: a migrated key resolves back to the original value."""
    _write_split(config_at, {"zhipu": {"apiKey": "sk-zhipu-original"}})
    migrate_plaintext_provider_keys(config_at)

    from durin.config.loader import load_config

    cfg = load_config(config_at)
    ref = cfg.providers.zhipu.api_key
    assert is_secret_ref(ref)
    assert resolve_secret(ref) == "sk-zhipu-original"
