"""Tests for the split-file config layout.

Users complained the monolithic ``config.json`` was both
- noisy (full of defaults they never touched), AND
- conceptually muddy (one file for `agents`, `providers`, `channels`,
  `gateway`, ... mixed together).

The split layout addresses both. Per-topic files live under
``~/.durin/config.json.d/``; the canonical ``config.json`` becomes a
1-line marker that tools can still read for backwards compatibility.
This file exercises:

1. Fresh installs write the split layout (no monolith).
2. Existing monolithic configs auto-migrate to split on first load.
3. Round-trip preserves all user-customised fields.
4. ``read_persisted_config`` works on either layout.
5. Stale split files get cleaned up when fields revert to defaults.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from durin.config.loader import (
    _is_split_layout,
    _split_dir,
    load_config,
    read_persisted_config,
    save_config,
)
from durin.config.schema import Config


def _seed_monolith(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Layout detection
# ---------------------------------------------------------------------------


def test_split_dir_is_dot_d_sibling_of_config_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    assert _split_dir(cfg).name == "config.json.d"
    assert _split_dir(cfg).parent == cfg.parent


def test_is_split_layout_false_when_no_dir(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    assert _is_split_layout(cfg) is False


def test_is_split_layout_true_after_save(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    _seed_monolith(cfg, {"agents": {"defaults": {"model": "glm-5.1"}}})
    load_config(cfg)  # migrates
    assert _is_split_layout(cfg) is True


# ---------------------------------------------------------------------------
# Migration from monolith to split
# ---------------------------------------------------------------------------


def test_load_migrates_existing_monolith_to_split(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    _seed_monolith(cfg, {
        "agents": {"defaults": {"model": "glm-5.1"}},
        "providers": {"zhipu": {"apiKey": "sk-test"}},
    })

    config = load_config(cfg)

    # Config object carries the user-set fields.
    assert config.agents.defaults.model == "glm-5.1"
    assert config.providers.zhipu.api_key == "sk-test"
    # Disk now has the split layout AND a `.legacy` backup of the old monolith.
    split_dir = _split_dir(cfg)
    assert split_dir.is_dir()
    assert (split_dir / "agents.json").exists()
    assert (split_dir / "providers.json").exists()
    legacy = cfg.with_suffix(".json.legacy")
    assert legacy.exists()
    # The canonical config.json now contains only the layout marker.
    marker = json.loads(cfg.read_text())
    assert marker == {"_layout": "split"}


def test_legacy_backup_is_not_overwritten_on_re_migration(tmp_path: Path) -> None:
    """If a .legacy file already exists (rare), we don't clobber it."""
    cfg = tmp_path / "config.json"
    _seed_monolith(cfg, {"agents": {"defaults": {"model": "glm-5.1"}}})
    # Plant a sentinel backup that pre-dates this run.
    legacy = cfg.with_suffix(".json.legacy")
    legacy.write_text(json.dumps({"sentinel": "old"}), encoding="utf-8")

    load_config(cfg)

    # The sentinel survives — we didn't overwrite it.
    assert json.loads(legacy.read_text()) == {"sentinel": "old"}


# ---------------------------------------------------------------------------
# Round-trip via the split layout
# ---------------------------------------------------------------------------


def test_save_then_load_preserves_user_fields(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    # Force split layout by creating the dir.
    _split_dir(cfg).mkdir(parents=True)

    config = Config()
    config.agents.defaults.provider = "zhipu"
    config.agents.defaults.model = "glm-5.1"
    config.providers.zhipu.api_key = "sk-roundtrip"
    save_config(config, cfg)

    loaded = load_config(cfg)
    assert loaded.agents.defaults.model == "glm-5.1"
    assert loaded.providers.zhipu.api_key == "sk-roundtrip"


def test_save_writes_one_file_per_top_level_key(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    _split_dir(cfg).mkdir(parents=True)

    config = Config()
    config.agents.defaults.model = "glm-5.1"
    config.providers.zhipu.api_key = "sk-x"
    save_config(config, cfg)

    split = _split_dir(cfg)
    # `agents` and `providers` were customised → they get their own file.
    assert (split / "agents.json").exists()
    assert (split / "providers.json").exists()
    # Sections the user didn't touch get nothing on disk (noise-free).
    assert not (split / "tools.json").exists()
    assert not (split / "api.json").exists()


def test_save_removes_stale_files_when_field_reverts_to_default(tmp_path: Path) -> None:
    """If a field was customised, then later cleared, the per-topic
    file shouldn't linger with its old contents."""
    cfg = tmp_path / "config.json"
    _split_dir(cfg).mkdir(parents=True)

    config = Config()
    config.agents.defaults.model = "glm-5.1"
    save_config(config, cfg)
    assert (_split_dir(cfg) / "agents.json").exists()

    # Now revert agents.defaults.model to the schema default — by
    # building a fresh Config and saving it, the agents file should
    # disappear because exclude_defaults strips everything.
    save_config(Config(), cfg)
    assert not (_split_dir(cfg) / "agents.json").exists()


# ---------------------------------------------------------------------------
# read_persisted_config helper
# ---------------------------------------------------------------------------


def test_read_persisted_config_reads_split_layout(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    _split_dir(cfg).mkdir(parents=True)
    (_split_dir(cfg) / "agents.json").write_text(
        json.dumps({"defaults": {"model": "glm-5.1"}}), encoding="utf-8",
    )

    data = read_persisted_config(cfg)
    assert data["agents"]["defaults"]["model"] == "glm-5.1"


def test_read_persisted_config_reads_monolith(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    _seed_monolith(cfg, {"agents": {"defaults": {"model": "glm-5.1"}}})

    data = read_persisted_config(cfg)
    assert data["agents"]["defaults"]["model"] == "glm-5.1"


def test_read_persisted_config_ignores_layout_marker(tmp_path: Path) -> None:
    """A monolith containing only `{_layout: split}` (post-migration
    marker) returns empty data when the split dir is missing — we
    don't want to feed the marker key into Pydantic."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"_layout": "split"}), encoding="utf-8")
    assert read_persisted_config(cfg) == {}


def test_read_persisted_config_empty_when_no_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    assert read_persisted_config(cfg) == {}


# ---------------------------------------------------------------------------
# backup_config — snapshot before a tool rewrites the config
# ---------------------------------------------------------------------------


def test_backup_config_copies_split_layout(tmp_path: Path) -> None:
    from durin.config.loader import backup_config

    cfg = tmp_path / "config.json"
    _split_dir(cfg).mkdir(parents=True)
    config = Config()
    config.agents.defaults.model = "glm-5.1"
    save_config(config, cfg)  # writes the split layout

    backup = backup_config(cfg)
    assert backup is not None and backup.is_dir()
    assert (backup / "agents.json").exists()


def test_backup_config_copies_monolith(tmp_path: Path) -> None:
    from durin.config.loader import backup_config

    cfg = tmp_path / "config.json"
    _seed_monolith(cfg, {"agents": {"defaults": {"model": "glm-5.1"}}})

    backup = backup_config(cfg)
    assert backup is not None and backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8"))["agents"]["defaults"]["model"] == "glm-5.1"


def test_backup_config_returns_none_when_nothing_on_disk(tmp_path: Path) -> None:
    from durin.config.loader import backup_config

    assert backup_config(tmp_path / "config.json") is None


# ---------------------------------------------------------------------------
# Noise pruning — enabled sections keep full attrs, disabled/empty are dropped
# ---------------------------------------------------------------------------


def test_prune_drops_empty_provider_sections() -> None:
    from durin.config.loader import _prune_noise_sections

    data = {
        "providers": {
            "zhipu": {"apiKey": "sk-real"},
            "openai": {"apiKey": None, "apiBase": None},
            "anthropic": {"apiKey": None, "apiBase": None, "extraHeaders": None},
        },
    }
    out = _prune_noise_sections(data)
    # Provider with a real key survives; all-null providers are dropped.
    assert "zhipu" in out["providers"]
    assert "openai" not in out["providers"]
    assert "anthropic" not in out["providers"]


def test_prune_drops_providers_key_when_all_empty() -> None:
    from durin.config.loader import _prune_noise_sections

    data = {"providers": {"openai": {"apiKey": None}, "anthropic": {"apiKey": None}}}
    out = _prune_noise_sections(data)
    assert "providers" not in out


def test_prune_keeps_enabled_channel_with_full_attrs() -> None:
    """An enabled channel is never pruned, even if every field is default."""
    from durin.config.loader import _prune_noise_sections

    data = {
        "channels": {
            "websocket": {"enabled": True, "host": "127.0.0.1", "port": 8765},
        },
    }
    out = _prune_noise_sections(data)
    assert out["channels"]["websocket"]["enabled"] is True
    assert out["channels"]["websocket"]["port"] == 8765


def test_prune_keeps_channel_scalar_settings() -> None:
    """Top-level channel scalars (sendProgress, …) are real settings, not noise."""
    from durin.config.loader import _prune_noise_sections

    data = {"channels": {"sendProgress": True, "transcriptionProvider": "groq"}}
    out = _prune_noise_sections(data)
    assert out["channels"]["sendProgress"] is True
    assert out["channels"]["transcriptionProvider"] == "groq"
