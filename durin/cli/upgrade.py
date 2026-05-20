"""`durin upgrade` — pull the latest build and re-run config migration.

Detects whether the running install is an editable checkout
(``pip install -e .``) or a regular wheel install, and dispatches to the
appropriate update path. After updating the package we re-run config
migration so any new schema defaults land in ``~/.durin/config.json``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console

from durin import __version__
from durin.config.loader import get_config_path, load_config, save_config

console = Console()

InstallMode = Literal["editable", "pipx", "wheel", "unknown"]

# PyPI distribution name (the import package + CLI command are still `durin`).
PYPI_DIST_NAME = "durin-agent"


@dataclass(frozen=True)
class InstallInfo:
    mode: InstallMode
    source_root: Path | None  # repo root for editable mode
    version: str


def detect_install_mode() -> InstallInfo:
    """Inspect the loaded ``durin`` package to figure out how it was installed.

    Recognised modes:
    - ``editable``: ``pip install -e .`` from a source checkout — there's a
      ``pyproject.toml`` alongside the loaded package.
    - ``pipx``: pipx puts the venv under ``~/.local/pipx/venvs/<pkg>/`` (or a
      ``PIPX_HOME``-rooted path). Detected by looking for ``/pipx/venvs/``
      anywhere on ``sys.executable``.
    - ``wheel``: regular ``pip install`` into a venv — package lives under
      ``site-packages/``.
    - ``unknown``: we couldn't pattern-match the path; the caller should bail
      with a clear message rather than guessing the upgrade command.
    """
    import durin

    pkg_path = Path(durin.__file__).resolve().parent  # …/<root>/durin/
    candidate_root = pkg_path.parent
    pyproject = candidate_root / "pyproject.toml"
    if pyproject.is_file():
        return InstallInfo(mode="editable", source_root=candidate_root, version=__version__)
    # pipx detection: the venv root lives under ``~/.local/pipx/venvs/<pkg>/``
    # (or a ``PIPX_HOME``-rooted variant). We probe ``sys.prefix`` and the raw
    # ``sys.executable`` *without* ``.resolve()`` — resolving follows the
    # symlink in ``<venv>/bin/python`` straight to the base interpreter
    # (e.g. Homebrew), which loses the ``/pipx/venvs/`` segment entirely.
    for candidate in (sys.prefix, sys.executable):
        s = str(candidate)
        if "/pipx/venvs/" in s or "\\pipx\\venvs\\" in s:
            return InstallInfo(mode="pipx", source_root=None, version=__version__)
    if "site-packages" in str(pkg_path):
        return InstallInfo(mode="wheel", source_root=None, version=__version__)
    return InstallInfo(mode="unknown", source_root=None, version=__version__)


def install_hint(extras: list[str], *, mode: InstallMode | None = None) -> str:
    """Return the right install command for the detected mode + a list of extras.

    ``extras`` is a list like ``["memory", "mcp"]``; pass ``[]`` for the
    bare-package command.
    """
    if mode is None:
        mode = detect_install_mode().mode
    bracket = f"[{','.join(extras)}]" if extras else ""
    spec = f"{PYPI_DIST_NAME}{bracket}"
    if mode == "editable":
        # Editable users want to re-install from the source tree.
        return f"pip install -e '.{bracket}'"
    if mode == "pipx":
        # `pipx install --force` should work but it's broken when pipx uses
        # `uv` as backend: `--force` doesn't translate to `uv venv --clear`,
        # so uv refuses to recreate the existing venv. Workaround:
        # uninstall first, then install fresh with the new extras.
        return f"pipx uninstall {PYPI_DIST_NAME} && pipx install '{spec}'"
    # `wheel` and `unknown` fall back to a regular pip command.
    return f"pip install --upgrade '{spec}'"


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> int:
    console.print(f"[dim]$ {' '.join(cmd)}{' (cwd=' + str(cwd) + ')' if cwd else ''}[/dim]")
    proc = subprocess.run(cmd, cwd=cwd, env=env)
    return proc.returncode


def pipx_subprocess_env() -> dict[str, str]:
    """Return the environment to use when invoking ``pipx``.

    pipx uses ``uv`` as backend on most modern installs, and uv aggressively
    caches the PyPI simple index. The cache survives ~15-30 min, so a
    freshly published release is invisible to ``pipx upgrade`` and
    ``pipx install`` until the cache expires (or you run ``uv cache
    clean``). ``UV_NO_CACHE=1`` disables the cache for this single
    subprocess call, adding ~2-3 seconds to the invocation but ensuring
    the user always sees the latest published version.

    Harmless when pipx isn't using uv (older pipx, or ``PIPX_DEFAULT_BACKEND=pip``):
    the env var is simply ignored.
    """
    return {**os.environ, "UV_NO_CACHE": "1"}


def _pull_editable(root: Path, ref: str | None) -> int:
    if not (root / ".git").exists():
        console.print(f"[red]No git checkout at {root}; cannot pull.[/red]")
        return 1
    if ref:
        if _run(["git", "-C", str(root), "fetch", "origin", ref]) != 0:
            return 1
        if _run(["git", "-C", str(root), "checkout", ref]) != 0:
            return 1
    rc = _run(["git", "-C", str(root), "pull", "--ff-only"])
    if rc != 0:
        return rc
    return _run([sys.executable, "-m", "pip", "install", "-e", str(root)])


def _upgrade_wheel() -> int:
    # The distribution name on PyPI is `durin-agent` (not `durin`).
    return _run([sys.executable, "-m", "pip", "install", "--upgrade", PYPI_DIST_NAME])


def _upgrade_pipx() -> int:
    """Run `pipx upgrade durin-agent`. Pre-releases require `--pip-args="--pre"`.

    The subprocess inherits a `UV_NO_CACHE=1` env var so a freshly-published
    release is visible even before uv's index cache expires.
    """
    return _run(["pipx", "upgrade", PYPI_DIST_NAME], env=pipx_subprocess_env())


def migrate_config_file(path: Path | None = None) -> bool:
    """Re-run schema migration on the existing config file.

    Returns ``True`` when the file changed on disk, ``False`` otherwise.
    Safe to call when no config exists yet — it then does nothing.
    """
    config_path = path or get_config_path()
    if not config_path.exists():
        return False
    before = config_path.read_text(encoding="utf-8")
    config = load_config(config_path)
    save_config(config, config_path)
    after = config_path.read_text(encoding="utf-8")
    return _normalize(before) != _normalize(after)


def _normalize(text: str) -> str:
    try:
        return json.dumps(json.loads(text), sort_keys=True)
    except json.JSONDecodeError:
        return text


def run_upgrade(
    *,
    check_only: bool = False,
    migrate_only: bool = False,
    ref: str | None = None,
) -> int:
    """Top-level entry; returns a process exit code."""
    info = detect_install_mode()
    console.print(f"[cyan]durin {info.version}[/cyan] ({info.mode})")

    if check_only:
        console.print("[dim]--check passed; not running pip.[/dim]")
        return 0

    if migrate_only:
        changed = migrate_config_file()
        if changed:
            console.print("[green]✓[/green] Config migration applied.")
        else:
            console.print("[dim]Config already up to date.[/dim]")
        return 0

    if info.mode == "editable":
        if info.source_root is None:
            console.print("[red]Editable mode detected but source root is unknown.[/red]")
            return 1
        rc = _pull_editable(info.source_root, ref)
    elif info.mode == "pipx":
        if ref:
            console.print("[yellow]--ref is only supported for editable installs; ignoring.[/yellow]")
        rc = _upgrade_pipx()
    elif info.mode == "wheel":
        if ref:
            console.print("[yellow]--ref is only supported for editable installs; ignoring.[/yellow]")
        rc = _upgrade_wheel()
    else:
        console.print(
            "[red]Unable to detect install mode.[/red] "
            "Run `pip install -e .` (editable) or `pip install --upgrade durin` (wheel) manually."
        )
        return 1

    if rc != 0:
        console.print(f"[red]Package update failed (exit {rc}).[/red]")
        return rc

    changed = migrate_config_file()
    if changed:
        console.print("[green]✓[/green] Config migrated to new schema defaults.")
    console.print("[green]✓[/green] Upgrade complete.")
    return 0


def register(app: typer.Typer) -> None:
    """Attach the `upgrade` command to a Typer app."""

    @app.command("upgrade")
    def upgrade(
        check: bool = typer.Option(False, "--check", help="Show install mode + version, then exit."),
        ref: str | None = typer.Option(None, "--ref", help="Git ref to check out (editable installs only)."),
        migrate_only: bool = typer.Option(False, "--migrate-only", help="Skip pip, only re-run config migration."),
    ) -> None:
        """Pull the latest build and re-run config migration."""
        rc = run_upgrade(check_only=check, migrate_only=migrate_only, ref=ref)
        if rc != 0:
            raise typer.Exit(rc)
