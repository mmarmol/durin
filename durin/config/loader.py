"""Configuration loading utilities.

Layout — split-file by topic:

    ~/.durin/
        config.json         # the canonical pointer (always returned by
                            # get_config_path); content is one of:
                            #   - empty {}  → mid-migration marker
                            #   - {"_layout": "split"} → split mode
                            #   - the full Config dict → legacy monolithic
        config/             # per-topic JSON files (split mode)
            agents.json
            providers.json
            channels.json
            memory.json
            gateway.json
            tools.json
            api.json
            install.json
            modelPresets.json
            modelCapabilities.json
        config.json.legacy  # backup of the pre-split monolith (created
                            # on first migration; never auto-deleted).

The split is what users see on disk: one file per topic, easy to grep
and edit by hand. The monolithic `config.json` stays around as a
1-line marker pointing at the split layout so existing tooling that
treats `config.json` as the canonical path (durin SDK, third-party
backups) doesn't blow up.

When ``save_config`` runs, only **non-default** fields are persisted
(``exclude_defaults=True``) so the user doesn't see hundreds of
``null`` / ``false`` lines for fields they never touched.
"""

import json
import os
import re
from pathlib import Path
from typing import Any

import pydantic
from loguru import logger
from pydantic import BaseModel

from durin.config.schema import Config

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None

# The split layout writes one file per top-level config section. The section
# set is NOT hardcoded — it is derived from the serialized config (every
# non-default top-level key), so a newly added Config section can never be
# silently dropped on save. (A hardcoded list previously lost telemetry /
# appearance / skills, each added after the list was frozen.)


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".durin" / "config.json"


def _split_dir(config_path: Path | None = None) -> Path:
    """The directory that holds per-topic split files.

    Uses the ``<filename>.d/`` convention so multiple config files in
    the same parent directory don't collide on the split layout — each
    config gets its own scoped dir. Example::

        ~/.durin/config.json        # marker after migration
        ~/.durin/config.json.d/     # split files (this directory)
            agents.json
            providers.json
            …
    """
    path = config_path or get_config_path()
    return path.with_suffix(path.suffix + ".d")


def _is_split_layout(config_path: Path | None = None) -> bool:
    """Return True when the split-file layout exists on disk."""
    return _split_dir(config_path).is_dir()


def read_persisted_config(config_path: Path | None = None) -> dict[str, Any]:
    """Return the on-disk config as a dict, transparent to layout.

    Useful for tests and tooling that want to inspect what got
    written without caring whether the canonical store is a single
    monolithic file or the split per-topic directory.
    """
    path = config_path or get_config_path()
    if _is_split_layout(path):
        return _read_split_layout(path)
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict) and tuple(data.keys()) == ("_layout",):
        return {}
    return data


def _migrate_to_split_layout(monolith_path: Path) -> None:
    """One-shot: split the monolith into per-topic files.

    Backs up the original as ``config.json.legacy`` and rewrites the
    canonical ``config.json`` as a tiny marker pointing at the split
    layout so downstream readers don't get confused.
    """
    try:
        raw = monolith_path.read_text(encoding="utf-8")
        data = json.loads(raw or "{}")
    except (OSError, json.JSONDecodeError):
        return
    # Already migrated? `_layout` marker means we shouldn't re-split.
    if isinstance(data, dict) and data.get("_layout") == "split":
        return
    split = _split_dir(monolith_path)
    split.mkdir(parents=True, exist_ok=True)
    for key, value in data.items():
        if key.startswith("_") or value is None:
            continue
        target = split / f"{key}.json"
        target.write_text(
            json.dumps(value, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    # Keep the old config as a backup, just renamed. The user can
    # always `mv config.json.legacy config.json` to revert.
    backup = monolith_path.with_suffix(".json.legacy")
    if not backup.exists():
        try:
            monolith_path.rename(backup)
        except OSError:
            pass
    # Marker file at the canonical path so tooling sees the layout.
    monolith_path.write_text(
        json.dumps({"_layout": "split"}, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_split_layout(config_path: Path) -> dict[str, Any]:
    """Read all per-topic files from the split dir and merge."""
    split = _split_dir(config_path)
    merged: dict[str, Any] = {}
    if not split.is_dir():
        return merged
    for path in sorted(split.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            continue
        key = path.stem
        try:
            with path.open(encoding="utf-8") as f:
                merged[key] = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Skipping unreadable split-config file {}: {}", path, e)
    return merged


def _write_split_layout(data: dict[str, Any], config_path: Path) -> None:
    """Write each top-level key to its own file under config/.

    Top-level keys with no content (or only default-derived empty
    dicts) are removed from disk to keep the layout clean — re-loading
    will fill those sections back in from Pydantic defaults.
    """
    split = _split_dir(config_path)
    split.mkdir(parents=True, exist_ok=True)
    seen: set[Path] = set()
    for key in data:
        if key.startswith("_"):
            continue
        target = split / f"{key}.json"
        seen.add(target)
        target.write_text(
            json.dumps(data[key], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    # Remove stale per-topic files (e.g. a field whose value reverted
    # to default). Otherwise their old contents would survive forever.
    for existing in split.iterdir():
        if existing.is_file() and existing.suffix == ".json" and existing not in seen:
            try:
                existing.unlink()
            except OSError:
                pass
    # Maintain the marker so `config.json` always returns split-mode info.
    config_path.write_text(
        json.dumps({"_layout": "split"}, indent=2) + "\n",
        encoding="utf-8",
    )


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    config = Config()

    # Path A: split layout already exists on disk → read each topic file.
    if _is_split_layout(path):
        data = _read_split_layout(path)
        if data:
            try:
                data = _migrate_config(data)
                config = Config.model_validate(data)
            except (ValueError, pydantic.ValidationError) as e:
                logger.warning("Failed to load split config: {}", e)
                logger.warning("Using default configuration.")
        _apply_ssrf_whitelist(config)
        return config

    # Path B: legacy monolith exists. Read it AND migrate to split
    # transparently so the next save lands in the new layout.
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # If the marker is the *only* thing in the monolith (e.g.
            # a half-migrated state where split dir was deleted), bail
            # to defaults rather than treating `_layout: split` as a
            # real config field.
            if isinstance(data, dict) and tuple(data.keys()) == ("_layout",):
                _apply_ssrf_whitelist(config)
                return config
            data = _migrate_config(data)
            config = Config.model_validate(data)
        except (json.JSONDecodeError, ValueError, pydantic.ValidationError) as e:
            logger.warning("Failed to load config from {}: {}", path, e)
            logger.warning("Using default configuration.")
        else:
            # Successful legacy load — migrate to split on the spot so
            # the user sees the new layout next time they look.
            try:
                _migrate_to_split_layout(path)
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not migrate config to split layout: {}", e)

    _apply_ssrf_whitelist(config)
    return config


def _apply_ssrf_whitelist(config: Config) -> None:
    """Apply SSRF whitelist from config to the network security module."""
    from durin.security.network import configure_ssrf_whitelist

    configure_ssrf_whitelist(config.tools.ssrf_whitelist)


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Persists only **non-default** fields (``exclude_defaults=True``)
    so the on-disk files stay short and readable. Layout is
    auto-detected:

    - If ``<root>/config/`` exists → write the split layout (per-topic
      files under that directory + a marker in ``config.json``).
    - Else → write the monolith and migrate to split on the next load.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    data = _prune_noise_sections(data)

    if _is_split_layout(path):
        _write_split_layout(data, path)
        return

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def backup_config(config_path: Path | None = None) -> Path | None:
    """Snapshot the current on-disk config before a tool rewrites it.

    Copies the split directory (or the legacy monolith) to a
    timestamped sibling so a botched `durin onboard` re-run can be
    reverted. Returns the backup path, or ``None`` when there is
    nothing on disk to back up.
    """
    import shutil
    from datetime import datetime

    path = config_path or get_config_path()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if _is_split_layout(path):
        src = _split_dir(path)
        dest = src.with_name(src.name + f".bak.{stamp}")
        try:
            shutil.copytree(src, dest)
        except OSError as e:
            logger.warning("Could not back up config dir: {}", e)
            return None
        return dest
    if path.exists():
        dest = path.with_suffix(path.suffix + f".bak.{stamp}")
        try:
            shutil.copy2(path, dest)
        except OSError as e:
            logger.warning("Could not back up config file: {}", e)
            return None
        return dest
    return None


def _all_values_empty(section: dict) -> bool:
    """True when every value in ``section`` is null / empty string / empty list/dict."""
    for v in section.values():
        if v is None:
            continue
        if isinstance(v, str) and v == "":
            continue
        if isinstance(v, (list, dict)) and len(v) == 0:
            continue
        return False
    return True


def _channel_matches_default(name: str, section: dict) -> bool:
    """True when a channel section is identical to that channel's shipped default.

    A disabled channel whose config equals ``default_config()`` carries
    zero user intent — it's pure noise from the old eager-inject of
    every discovered channel. Such sections are dropped on save.
    """
    try:
        from durin.channels.registry import discover_all

        cls = discover_all().get(name)
        if cls is None:
            return False
        default = cls.default_config()
    except Exception:  # noqa: BLE001
        return False
    return section == default


def _prune_noise_sections(data: dict) -> dict:
    """Drop config sub-sections the user never meaningfully configured.

    The user's rule: unconfigured / disabled things should be ABSENT
    from the file; things that are *enabled* keep their full attribute
    set so they stay discoverable + editable.

    - ``providers.<name>``: dropped when every field is null/empty (no
      api key, no base url — the provider was never set up).
    - ``channels.<name>``: dropped when the channel is not enabled AND
      its section equals the shipped ``default_config()``. Enabled
      channels are kept verbatim (full attributes).
    """
    providers = data.get("providers")
    if isinstance(providers, dict):
        for pname in list(providers.keys()):
            section = providers[pname]
            if isinstance(section, dict) and _all_values_empty(section):
                del providers[pname]
        if not providers:
            del data["providers"]

    channels = data.get("channels")
    if isinstance(channels, dict):
        for cname in list(channels.keys()):
            section = channels[cname]
            # Top-level scalars (sendProgress, transcriptionProvider, …)
            # are not channel sub-sections — leave them alone.
            if not isinstance(section, dict):
                continue
            if section.get("enabled"):
                continue  # enabled → keep full attributes
            if _channel_matches_default(cname, section):
                del channels[cname]
        # Keep the `channels` key even if only scalars remain — those
        # are real settings, not noise.

    return data


_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_config_env_vars(config: Config) -> Config:
    """Return *config* with ``${VAR}`` env-var references resolved.

    Walks in place so fields declared with ``exclude=True`` survive;
    returns the same instance when no references are present. Raises
    ``ValueError`` if a referenced variable is not set.
    """
    return _resolve_in_place(config)


def _resolve_in_place(obj: Any) -> Any:
    if isinstance(obj, str):
        new = _ENV_REF_PATTERN.sub(_env_replace, obj)
        return new if new != obj else obj
    if isinstance(obj, BaseModel):
        updates: dict[str, Any] = {}
        for name in type(obj).model_fields:
            old = getattr(obj, name)
            new = _resolve_in_place(old)
            if new is not old:
                updates[name] = new
        extras = obj.__pydantic_extra__
        new_extras: dict[str, Any] | None = None
        if extras:
            resolved = {k: _resolve_in_place(v) for k, v in extras.items()}
            if any(resolved[k] is not extras[k] for k in extras):
                new_extras = resolved
        if not updates and new_extras is None:
            return obj
        copy = obj.model_copy(update=updates) if updates else obj.model_copy()
        if new_extras is not None:
            copy.__pydantic_extra__ = new_extras
        return copy
    if isinstance(obj, dict):
        resolved = {k: _resolve_in_place(v) for k, v in obj.items()}
        return resolved if any(resolved[k] is not obj[k] for k in obj) else obj
    if isinstance(obj, list):
        resolved = [_resolve_in_place(v) for v in obj]
        return resolved if any(nv is not ov for nv, ov in zip(resolved, obj)) else obj
    return obj


def _resolve_env_vars(obj: object) -> object:
    """Recursively resolve ``${VAR}`` patterns in plain strings/dicts/lists."""
    if isinstance(obj, str):
        return _ENV_REF_PATTERN.sub(_env_replace, obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _env_replace(match: re.Match[str]) -> str:
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ValueError(
            f"Environment variable '{name}' referenced in config is not set"
        )
    return value


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # Move tools.myEnabled / tools.mySet → tools.my.{enable, allowSet}.
    # The old flat keys shipped in the initial MyTool landing; wrapping them in a
    # sub-config keeps `web` / `exec` / `my` symmetric and gives room to grow.
    if "myEnabled" in tools or "mySet" in tools:
        my_cfg = tools.setdefault("my", {})
        if "myEnabled" in tools and "enable" not in my_cfg:
            my_cfg["enable"] = tools.pop("myEnabled")
        else:
            tools.pop("myEnabled", None)
        if "mySet" in tools and "allowSet" not in my_cfg:
            my_cfg["allowSet"] = tools.pop("mySet")
        else:
            tools.pop("mySet", None)

    # Move memory.skillImport → skills.security and memory.skillsHotTier →
    # agents.defaults.skillsHotTier (spec 2026-06-03 §9 — skills config reorg).
    # Handles both camelCase (as persisted) and snake_case keys.
    memory = data.get("memory", {})
    for legacy in ("skillImport", "skill_import"):
        if legacy in memory:
            security = data.setdefault("skills", {}).setdefault("security", {})
            for key, value in memory.pop(legacy).items():
                security.setdefault(key, value)
            break
    for legacy in ("skillsHotTier", "skills_hot_tier"):
        if legacy in memory:
            defaults = data.setdefault("agents", {}).setdefault("defaults", {})
            defaults.setdefault("skillsHotTier", memory.pop(legacy))
            break

    return data
