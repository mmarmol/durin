"""The telemetry directory follows the instance home.

``$DURIN_HOME`` selects a self-contained durin instance; telemetry is
instance data, so an explicit instance home carries its own
``telemetry/`` directory. Without ``DURIN_HOME`` the default install keeps
writing under ``~/.cache/durin/telemetry`` as before. Every reader and
writer resolves the directory through the same function, so a test, a
script or a second instance never lands in the live directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from durin.config.paths import get_telemetry_dir
from durin.telemetry.logger import get_session_logger


def test_explicit_instance_home_owns_its_telemetry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DURIN_HOME", str(tmp_path / "instance"))
    assert get_telemetry_dir() == tmp_path / "instance" / "telemetry"


def test_default_install_keeps_the_cache_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DURIN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    assert get_telemetry_dir() == tmp_path / "user" / ".cache" / "durin" / "telemetry"


def test_session_logger_writes_under_the_resolved_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DURIN_HOME", str(tmp_path / "instance"))
    logger = get_session_logger("websocket:c")
    assert logger._path.parent == tmp_path / "instance" / "telemetry"
    assert logger._path.name.startswith("websocket_c_")


def test_an_explicit_base_dir_still_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DURIN_HOME", str(tmp_path / "instance"))
    logger = get_session_logger("cli:test", base_dir=tmp_path / "elsewhere")
    assert logger._path.parent == tmp_path / "elsewhere"


@pytest.mark.parametrize(
    "resolver",
    [
        "durin.memory.stats:default_telemetry_dir",
        "durin.service.memory:_telemetry_dir",
    ],
)
def test_readers_resolve_the_same_directory(resolver: str, tmp_path: Path, monkeypatch) -> None:
    """The stats aggregator and the webui logs reader read where the logger writes."""
    import importlib

    monkeypatch.setenv("DURIN_HOME", str(tmp_path / "instance"))
    module_name, func_name = resolver.split(":")
    func = getattr(importlib.import_module(module_name), func_name)
    assert func() == tmp_path / "instance" / "telemetry"
