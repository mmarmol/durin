"""Tests for extras-drift detection in `durin doctor`.

These verify the two-layer install-UX guarantee:

1. `config.install.extras` accumulates monotonically — once `memory`
   is seen importable, it stays in the tracked set even if pipx wipes
   it later.

2. `durin doctor` surfaces a `warn` whenever something tracked is no
   longer importable, with a fix string the user can copy.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from durin.cli.doctor import (
    check_extras_drift,
    detect_installed_extras,
    update_extras_state,
)
from durin.config.schema import Config


@pytest.fixture
def temp_config(tmp_path: Path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(Config().model_dump(mode="json", by_alias=True), indent=2),
        encoding="utf-8",
    )
    with patch("durin.cli.doctor.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path):
        yield cfg_path


def _read_extras(cfg_path: Path) -> list[str]:
    """Layout-aware: works for both monolithic and split config files."""
    from durin.config.loader import read_persisted_config

    return read_persisted_config(cfg_path).get("install", {}).get("extras", [])


def test_detect_installed_extras_returns_list() -> None:
    """The detector must return probe-table extras only, and every probe-table
    entry must be an extra pyproject actually declares — a hardcoded name list
    here would itself drift from the probe table (it did: `tts`)."""
    import tomllib

    from durin.cli.doctor import _EXTRAS_IMPORT_PROBES

    found = detect_installed_extras()
    assert set(found).issubset(_EXTRAS_IMPORT_PROBES)

    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))[
        "project"]["optional-dependencies"]
    assert set(_EXTRAS_IMPORT_PROBES).issubset(declared)


def test_update_extras_state_adds_new(temp_config: Path) -> None:
    """When `detect_installed_extras` reports new extras, they get appended."""
    with patch("durin.cli.doctor.detect_installed_extras", return_value=["memory", "mcp"]):
        update_extras_state()
    assert set(_read_extras(temp_config)) == {"memory", "mcp"}


def test_update_extras_state_is_additive_not_subtractive(temp_config: Path) -> None:
    """If a previously-tracked extra is gone, the state must NOT drop it.

    This is the core of the install-UX guarantee: durin remembers that
    you used to have memory, so it can warn you after a pipx reinstall.
    """
    # First pass: memory + mcp are present.
    with patch("durin.cli.doctor.detect_installed_extras", return_value=["memory", "mcp"]):
        update_extras_state()
    assert set(_read_extras(temp_config)) == {"memory", "mcp"}

    # Second pass: only mcp present (user uninstalled memory).
    with patch("durin.cli.doctor.detect_installed_extras", return_value=["mcp"]):
        update_extras_state()
    # Tracked set should STILL contain memory.
    assert set(_read_extras(temp_config)) == {"memory", "mcp"}


def test_update_extras_state_does_not_save_when_unchanged(temp_config: Path) -> None:
    """No-op update must NOT write to disk."""
    with patch("durin.cli.doctor.detect_installed_extras", return_value=[]):
        result = update_extras_state()
    assert result is None  # no update happened
    assert _read_extras(temp_config) == []


def test_check_drift_silent_when_no_extras_tracked(temp_config: Path) -> None:
    r = check_extras_drift()
    assert r.status == "ok"
    assert "none tracked" in r.message.lower()
    assert r.name == "previously installed extras"


def test_check_drift_ok_when_all_tracked_present(temp_config: Path) -> None:
    """If everything tracked is currently importable, drift check is green."""
    # Seed config: tracked = [memory]
    data = json.loads(temp_config.read_text())
    data.setdefault("install", {})["extras"] = ["memory"]
    temp_config.write_text(json.dumps(data), encoding="utf-8")

    with patch("durin.cli.doctor.detect_installed_extras", return_value=["memory"]):
        r = check_extras_drift()
    assert r.status == "ok"
    assert "1 present" in r.message
    assert "memory" in r.message


def test_check_drift_warn_when_tracked_is_missing(temp_config: Path) -> None:
    """If config says memory was here but `import fastembed` fails, warn."""
    data = json.loads(temp_config.read_text())
    data.setdefault("install", {})["extras"] = ["memory", "mcp"]
    temp_config.write_text(json.dumps(data), encoding="utf-8")

    # Detector says only mcp is currently present.
    with patch("durin.cli.doctor.detect_installed_extras", return_value=["mcp"]):
        r = check_extras_drift()
    assert r.status == "warn"
    assert "memory" in r.message
    assert "mcp" not in r.message  # mcp is still there, only memory is gone
    assert r.fix and "install-missing" in r.fix


def test_check_drift_warn_multi_missing(temp_config: Path) -> None:
    data = json.loads(temp_config.read_text())
    data.setdefault("install", {})["extras"] = ["memory", "mcp", "web"]
    temp_config.write_text(json.dumps(data), encoding="utf-8")

    with patch("durin.cli.doctor.detect_installed_extras", return_value=[]):
        r = check_extras_drift()
    assert r.status == "warn"
    # All three should be mentioned in the message.
    for name in ("memory", "mcp", "web"):
        assert name in r.message


def test_detect_installed_extras_smoke_real() -> None:
    """Smoke: the function runs against the real importlib without raising."""
    # No assertion about which extras are present — that depends on the
    # dev environment. Just verifies the probe doesn't raise.
    result = detect_installed_extras()
    assert isinstance(result, list)
    assert all(isinstance(x, str) for x in result)
