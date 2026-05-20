"""`durin upgrade` — pull the latest build and re-run config migration.

Detects whether the running install is an editable checkout
(``pip install -e .``) or a regular wheel install, and dispatches to the
appropriate update path. After updating the package we re-run config
migration so any new schema defaults land in ``~/.durin/config.json``.
"""

from __future__ import annotations

import json
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

InstallMode = Literal["editable", "wheel", "unknown"]


@dataclass(frozen=True)
class InstallInfo:
    mode: InstallMode
    source_root: Path | None  # repo root for editable mode
    version: str


def detect_install_mode() -> InstallInfo:
    """Inspect the loaded ``durin`` package to figure out how it was installed.

    Editable installs leave ``__file__`` inside the source tree (alongside a
    ``pyproject.toml``). Regular wheel installs live under ``site-packages``.
    """
    import durin

    pkg_path = Path(durin.__file__).resolve().parent  # …/<root>/durin/
    candidate_root = pkg_path.parent
    pyproject = candidate_root / "pyproject.toml"
    if pyproject.is_file():
        return InstallInfo(mode="editable", source_root=candidate_root, version=__version__)
    if "site-packages" in str(pkg_path):
        return InstallInfo(mode="wheel", source_root=None, version=__version__)
    return InstallInfo(mode="unknown", source_root=None, version=__version__)


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    console.print(f"[dim]$ {' '.join(cmd)}{' (cwd=' + str(cwd) + ')' if cwd else ''}[/dim]")
    proc = subprocess.run(cmd, cwd=cwd)
    return proc.returncode


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
    return _run([sys.executable, "-m", "pip", "install", "--upgrade", "durin"])


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
