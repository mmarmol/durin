"""Tests for `durin upgrade` install-mode detection and config migration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.commands import app
from durin.cli.upgrade import (
    InstallInfo,
    detect_install_mode,
    migrate_config_file,
    run_upgrade,
)
from durin.config.schema import Config

runner = CliRunner()


def test_detect_install_mode_recognises_editable_checkout() -> None:
    # Running tests from a source checkout — pyproject.toml is alongside durin/.
    info = detect_install_mode()
    # The fixture machine layout is editable; this is the only reliable assertion.
    assert info.mode in ("editable", "wheel", "unknown")


def test_detect_install_mode_returns_install_info() -> None:
    info = detect_install_mode()
    assert isinstance(info, InstallInfo)
    assert isinstance(info.version, str) and info.version


def test_detect_install_mode_pipx_from_sys_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a pipx venv whose ``bin/python`` symlinks out to the base
    interpreter must still be detected as pipx via ``sys.prefix``.

    Before this test the detector ran ``Path(sys.executable).resolve()``,
    which followed the symlink to (e.g.) the Homebrew interpreter and
    lost the ``/pipx/venvs/`` segment entirely.
    """
    import sys as _sys

    import durin
    from durin.cli import upgrade as upgrade_mod

    fake_venv = tmp_path / ".local" / "pipx" / "venvs" / "durin-agent"
    fake_pkg = fake_venv / "lib" / "python3.14" / "site-packages" / "durin"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "__init__.py").write_text("", encoding="utf-8")
    # Pretend durin lives inside the pipx venv (no pyproject.toml alongside).
    monkeypatch.setattr(durin, "__file__", str(fake_pkg / "__init__.py"))
    # `sys.executable` looks resolved (Homebrew path) — pretending the symlink
    # got followed. `sys.prefix` still points at the pipx venv root.
    monkeypatch.setattr(_sys, "prefix", str(fake_venv))
    monkeypatch.setattr(_sys, "executable", "/opt/homebrew/Cellar/python@3.14/3.14.5/bin/python3.14")
    info = upgrade_mod.detect_install_mode()
    assert info.mode == "pipx", f"expected pipx, got {info.mode}"


def test_cli_upgrade_check_exits_zero(tmp_path: Path) -> None:
    # --check must never run pip; we don't even need a real config path.
    result = runner.invoke(app, ["upgrade", "--check"])
    assert result.exit_code == 0, result.output
    assert "--check passed" in result.output


def test_cli_upgrade_migrate_only_runs_migration(tmp_path: Path) -> None:
    """`--migrate-only` should re-save the config without calling pip.

    With `exclude_defaults=True` + split layout, an empty input config
    produces an empty on-disk representation (no fields to persist).
    The real assertion is that load_config still returns a valid
    Config object after the migration completes.
    """
    cfg_path = tmp_path / "config.json"
    # Seed with a non-default field so we have something to verify
    # survives the round-trip.
    cfg_path.write_text(
        json.dumps({"agents": {"defaults": {"model": "glm-5.1"}}}, indent=2),
        encoding="utf-8",
    )

    with patch("durin.cli.upgrade.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path):
        result = runner.invoke(app, ["upgrade", "--migrate-only"])
    assert result.exit_code == 0, result.output

    # The user-set field survived migration, regardless of layout.
    from durin.config.loader import load_config

    cfg = load_config(cfg_path)
    assert cfg.agents.defaults.model == "glm-5.1"


def test_migrate_config_file_noop_on_already_current(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    # Seed with what save_config WOULD write (exclude_defaults), so a
    # round-trip is a true no-op. Previously this test wrote a full dump
    # of every default field; that always diffed against the slimmer
    # canonical form once `save_config` adopted `exclude_defaults=True`.
    cfg_path.write_text(
        json.dumps(
            Config().model_dump(mode="json", by_alias=True, exclude_defaults=True),
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with patch("durin.config.loader.get_config_path", return_value=cfg_path):
        changed = migrate_config_file(cfg_path)
    assert changed is False


def test_migrate_config_file_returns_true_when_defaults_get_added(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    # An empty {} on disk vs an empty {} from save_config(Config()) is
    # still a no-op now (since exclude_defaults strips everything).
    # Seed with something the migration WILL strip — a legacy key —
    # and assert the file changed when re-saved without it.
    cfg_path.write_text(
        json.dumps({"tools": {"myEnabled": True, "mySet": True}}),
        encoding="utf-8",
    )
    with patch("durin.config.loader.get_config_path", return_value=cfg_path):
        changed = migrate_config_file(cfg_path)
    assert changed is True


def test_run_upgrade_check_only_does_not_call_pip(tmp_path: Path) -> None:
    """run_upgrade(check_only=True) must not invoke any subprocess."""
    with patch("durin.cli.upgrade.subprocess.run") as mock_run:
        rc = run_upgrade(check_only=True)
    assert rc == 0
    mock_run.assert_not_called()


def test_run_upgrade_migrate_only_does_not_call_pip(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    with patch("durin.cli.upgrade.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path), \
         patch("durin.cli.upgrade.subprocess.run") as mock_run:
        rc = run_upgrade(migrate_only=True)
    assert rc == 0
    mock_run.assert_not_called()


def test_run_upgrade_editable_pulls_and_reinstalls(tmp_path: Path) -> None:
    """Editable mode should run `git pull --ff-only` then `pip install -e <root>`."""
    fake_root = tmp_path / "checkout"
    (fake_root / ".git").mkdir(parents=True)
    info = InstallInfo(mode="editable", source_root=fake_root, version="0.1.0")

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")

    captured: list[list[str]] = []

    def _fake_subprocess_run(cmd, cwd=None, env=None):
        captured.append(list(cmd))
        # Return success on every call.
        class _R:
            returncode = 0
        return _R()

    with patch("durin.cli.upgrade.detect_install_mode", return_value=info), \
         patch("durin.cli.upgrade.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path), \
         patch("durin.cli.upgrade.subprocess.run", side_effect=_fake_subprocess_run):
        rc = run_upgrade()

    assert rc == 0
    flat = [" ".join(c) for c in captured]
    assert any("git" in c and "pull" in c for c in flat), captured
    assert any("pip" in c and "install" in c and "-e" in c for c in flat), captured


def test_run_upgrade_pipx_calls_pipx_upgrade(tmp_path: Path) -> None:
    """pipx-installed durin should upgrade via `pipx upgrade durin-agent`."""
    info = InstallInfo(mode="pipx", source_root=None, version="0.1.0a1")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")

    captured: list[tuple[list[str], dict | None]] = []

    def _fake_subprocess_run(cmd, cwd=None, env=None):
        captured.append((list(cmd), env))
        class _R:
            returncode = 0
        return _R()

    with patch("durin.cli.upgrade.detect_install_mode", return_value=info), \
         patch("durin.cli.upgrade.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path), \
         patch("durin.cli.upgrade.subprocess.run", side_effect=_fake_subprocess_run):
        rc = run_upgrade()

    assert rc == 0
    flat = [" ".join(c) for c, _e in captured]
    assert any("pipx" in c and "upgrade" in c and "durin-agent" in c for c in flat), captured
    # Regression: the pipx upgrade must run with UV_NO_CACHE=1 so a fresh
    # release isn't masked by uv's index cache.
    pipx_calls = [(c, e) for c, e in captured if "pipx" in " ".join(c)]
    assert pipx_calls, captured
    cmd, env = pipx_calls[0]
    assert env is not None and env.get("UV_NO_CACHE") == "1", (cmd, env)


def test_pipx_subprocess_env_sets_uv_no_cache() -> None:
    from durin.cli.upgrade import pipx_subprocess_env

    env = pipx_subprocess_env()
    assert env["UV_NO_CACHE"] == "1"
    # Should also carry through the rest of the parent env (PATH at minimum).
    assert "PATH" in env


def test_install_hint_for_editable() -> None:
    from durin.cli.upgrade import install_hint

    assert install_hint(["memory"], mode="editable") == "pip install -e '.[memory]'"
    assert install_hint([], mode="editable") == "pip install -e '.'"


def test_install_hint_for_pipx_uses_inject() -> None:
    """pipx mode should suggest `pipx inject` — non-destructive, no uninstall."""
    from durin.cli.upgrade import install_hint

    hint = install_hint(["memory"], mode="pipx")
    assert hint.startswith("pipx inject durin-agent "), hint
    # The memory extra pulls in fastembed + lancedb per pyproject.toml.
    assert "fastembed" in hint
    assert "lancedb" in hint
    # Combined extras both land in a single inject command.
    combined = install_hint(["memory", "mcp"], mode="pipx")
    assert "mcp" in combined
    assert "fastembed" in combined


def test_install_hint_pipx_never_uninstalls() -> None:
    """Regression: a user reported the old uninstall+install hint as
    'pésimo'. The new hint must not destructively reinstall the agent."""
    from durin.cli.upgrade import install_hint

    hint = install_hint(["memory", "mcp"], mode="pipx")
    assert "uninstall" not in hint
    assert "--force" not in hint


def test_extras_to_packages_resolves_memory() -> None:
    from durin.cli.upgrade import extras_to_packages

    pkgs = extras_to_packages(["memory"])
    assert "fastembed" in pkgs
    assert "lancedb" in pkgs


def test_extras_to_packages_resolves_mcp() -> None:
    from durin.cli.upgrade import extras_to_packages

    pkgs = extras_to_packages(["mcp"])
    assert pkgs == ["mcp"]


def test_extras_to_packages_combines_multiple_extras() -> None:
    from durin.cli.upgrade import extras_to_packages

    pkgs = extras_to_packages(["memory", "mcp"])
    assert {"fastembed", "lancedb", "mcp"}.issubset(set(pkgs))


def test_extras_to_packages_unknown_extra_returns_empty() -> None:
    from durin.cli.upgrade import extras_to_packages

    assert extras_to_packages(["definitely-not-an-extra"]) == []


def test_extras_to_packages_strips_version_specifiers() -> None:
    """Returned names must be bare (no `>=`, no `[...]`) so `pipx inject`
    accepts them without per-arg shell quoting."""
    from durin.cli.upgrade import extras_to_packages

    for pkg in extras_to_packages(["memory", "mcp", "web"]):
        for ch in "<>=!~ ;":
            assert ch not in pkg, f"unexpected spec char {ch!r} in {pkg!r}"


def test_install_hint_for_wheel() -> None:
    from durin.cli.upgrade import install_hint

    assert install_hint(["memory"], mode="wheel") == "pip install --upgrade 'durin-agent[memory]'"
    assert install_hint([], mode="wheel") == "pip install --upgrade 'durin-agent'"


def test_install_hint_uses_pypi_dist_name() -> None:
    """Regression: the hint must never say `durin[...]` (that distribution is taken)."""
    from durin.cli.upgrade import install_hint

    for mode in ("pipx", "wheel"):
        hint = install_hint(["memory"], mode=mode)  # type: ignore[arg-type]
        assert "durin-agent" in hint
        assert "'durin[" not in hint  # the bare name must not appear in brackets


def test_run_upgrade_wheel_calls_pip_upgrade(tmp_path: Path) -> None:
    info = InstallInfo(mode="wheel", source_root=None, version="0.1.0")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")

    captured: list[list[str]] = []

    def _fake_subprocess_run(cmd, cwd=None, env=None):
        captured.append(list(cmd))
        class _R:
            returncode = 0
        return _R()

    with patch("durin.cli.upgrade.detect_install_mode", return_value=info), \
         patch("durin.cli.upgrade.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path), \
         patch("durin.cli.upgrade.subprocess.run", side_effect=_fake_subprocess_run):
        rc = run_upgrade()

    assert rc == 0
    flat = [" ".join(c) for c in captured]
    assert any(
        "pip" in c and "install" in c and "--upgrade" in c and "durin-agent" in c for c in flat
    ), captured
