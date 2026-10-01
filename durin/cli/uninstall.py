"""`durin uninstall` — enumerate durin's on-disk state and remove it.

Two-phase by design: enumerate first (always safe), prompt with the exact
paths + byte counts, then delete. `--purge` additionally `pip uninstall`s
the package itself in a subprocess so the running command can exit cleanly
before the package goes away.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from durin.cli.upgrade import PYPI_DIST_NAME

console = Console()


@dataclass(frozen=True)
class TargetGroup:
    """A logical group of paths that share a single `--keep-*` flag."""

    name: str
    keep_flag: str  # e.g. "--keep-config"
    paths: tuple[Path, ...]


# What ~/.cache/durin holds that uninstall knows, and why an instance selected
# with DURIN_HOME leaves each: none of it is the instance's own.
_DEFAULT_CACHE = (
    ("telemetry", "the default install's telemetry"),
    ("models", "model files shared by every install on this machine"),
    ("archive", "the default install's archive"),
)


def _default_cache() -> tuple[Path, bool]:
    """``~/.cache/durin``, and whether this install owns it: the default
    install does, its telemetry lives there; an instance selected with
    DURIN_HOME keeps its telemetry in its own home and owns none of it."""
    from durin.config.paths import get_telemetry_dir

    cache = Path.home() / ".cache" / "durin"
    return cache, get_telemetry_dir() == cache / "telemetry"


_MARKERS = 'config.json.d/, or a config.json that is durin\'s split-layout marker {"_layout": "split"}'


def _refusal(durin_home: Path) -> str | None:
    """Why uninstall must not touch *durin_home* at all, or None.

    The user's home folder, the filesystem root and any folder that contains
    the user's home are never a durin home, whatever DURIN_HOME says. Nor is
    a folder without durin's markers: the names durin uses there
    (``config.json``, ``logs/``, ``media/``, ``sessions/``…) may be the
    user's own, so nothing in it is removed; a durin home that lost its
    markers is removed by hand."""
    home = Path.home().resolve()
    target = durin_home.expanduser().resolve()
    if target == home:
        return f"{durin_home} is your home folder, not a durin home. Point DURIN_HOME at the durin home itself."
    if home.is_relative_to(target):
        return (
            f"{durin_home} contains your home folder: it is not a durin home. "
            "Point DURIN_HOME at the durin home itself."
        )
    if target.is_dir() and not _is_durin_home(target):
        return (
            f"{durin_home} holds none of durin's markers ({_MARKERS}), so it is not recognizably "
            "a durin home. If it is one, remove it by hand."
        )
    return None


def _is_durin_home(durin_home: Path) -> bool:
    """Whether *durin_home* is recognizably durin's: it holds the split
    config's sections folder (``config.json.d/``) or a ``config.json`` that
    is the split layout's marker, which every durin release writes. Names
    like ``config.json`` or ``workspace/`` alone are common in other folders."""
    from durin.config.loader import _split_dir

    config = durin_home / "config.json"
    if _split_dir(config).is_dir():
        return True
    try:
        return json.loads(config.read_text(encoding="utf-8")) == {"_layout": "split"}
    except (OSError, ValueError):
        return False


def left_in_place() -> list[tuple[Path, str]]:
    """What uninstall leaves in ``~/.cache/durin`` for an instance, with why;
    nothing for the default install, which removes it."""
    cache, owned = _default_cache()
    if owned:
        return []
    return [(cache / name, why) for name, why in _DEFAULT_CACHE if (cache / name).exists()]


def _config_paths(durin_home: Path) -> tuple[Path, ...]:
    """The config, its backups, and the credentials its ``${secret:…}``
    references and sign-ins resolve against: removed together and kept
    together, since a config kept without them names secrets it can no
    longer reach."""
    from durin.config.loader import _split_dir

    config = durin_home / "config.json"
    split = _split_dir(config)
    return (
        config,
        # The split layout's sections: config.json is then only its marker.
        split,
        durin_home / "config.json.bak",
        # The timestamped copies a rewrite takes first, and the monolith the
        # move to the split layout kept.
        *sorted(durin_home.glob(f"{config.name}.bak.*")),
        *sorted(durin_home.glob(f"{split.name}.bak.*")),
        durin_home / "config.json.legacy",
        durin_home / "pairing.json",
        durin_home / "secrets.json",
        durin_home / "api_tokens.json",
        durin_home / "oauth",
    )


def default_target_groups(workspace: Path | None = None) -> list[TargetGroup]:
    """Return the groups of paths uninstall touches by default.

    ``workspace`` is opt-in: per-workspace scratch directories live next to
    project code and are never removed unless the user names a workspace
    explicitly. Raises ``OSError`` when the durin home cannot be listed.
    """
    from durin.config.home import durin_home as _durin_home_root
    from durin.config.paths import get_telemetry_dir

    durin_home = _durin_home_root()
    cache, owned = _default_cache()

    config_paths = _config_paths(durin_home)
    workspace_paths = (durin_home / "workspace",)
    # This install's own telemetry, wherever it lives, the model caches in its
    # home (STT engines, OCR languages), and the rest of ~/.cache/durin only
    # when that is this install's.
    cache_paths = tuple(dict.fromkeys((
        get_telemetry_dir(),
        durin_home / "telemetry",
        durin_home / "models",
        *((cache / "models", cache / "archive") if owned else ()),
    )))
    # Everything else in the durin home, whatever its name: a list of known
    # names leaves behind whatever durin writes under a name it lacks. Only
    # in a folder that is recognizably durin's; uninstall refuses any other.
    claimed = {*config_paths, *workspace_paths, *cache_paths}
    entries = sorted(durin_home.iterdir()) if _is_durin_home(durin_home) else []
    other_paths = tuple(path for path in entries if path not in claimed)

    groups = [
        TargetGroup("Config", "--keep-config", config_paths),
        TargetGroup("Workspace", "--keep-workspace", workspace_paths),
        TargetGroup("Cache", "--keep-cache", cache_paths),
        TargetGroup("Other state", "", other_paths),
    ]
    if workspace is not None:
        scratch = workspace / ".durin"
        groups.append(TargetGroup("Per-workspace scratch", "", (scratch,)))
    return groups


def _path_size(path: Path) -> int:
    """Recursive byte count; returns 0 for missing paths. A symlink counts
    as the link itself: what uninstall removes of it."""
    if path.is_symlink():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            p = Path(root) / name
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


def _format_bytes(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} {u}"
        f /= 1024
    return f"{n} B"


def _delete(path: Path) -> bool:
    """Remove ``path``; True once it is gone, also when something else
    removed it first (stopping the gateway removes its pid file). A symlink
    goes as the link itself, and the tree removal never follows one, so
    nothing outside the path is touched."""
    with contextlib.suppress(OSError):
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
    # Judged by the outcome: a removal that stopped partway leaves the path.
    return not (path.exists() or path.is_symlink())


def collect_targets(
    *,
    keep_config: bool,
    keep_workspace: bool,
    keep_cache: bool,
    workspace: Path | None = None,
) -> list[tuple[TargetGroup, Path, int]]:
    """Return (group, path, size) tuples for everything that would be deleted."""
    out: list[tuple[TargetGroup, Path, int]] = []
    for group in default_target_groups(workspace):
        if keep_config and group.keep_flag == "--keep-config":
            continue
        if keep_workspace and group.keep_flag == "--keep-workspace":
            continue
        if keep_cache and group.keep_flag == "--keep-cache":
            continue
        for path in group.paths:
            if not path.exists() and not path.is_symlink():
                continue
            out.append((group, path, _path_size(path)))
    return out


def _render_plan(targets: list[tuple[TargetGroup, Path, int]], left: list[tuple[Path, str]]) -> None:
    if targets:
        table = Table(title="durin uninstall plan", show_lines=False)
        table.add_column("Group")
        table.add_column("Path")
        table.add_column("Size", justify="right")
        total = 0
        for group, path, size in targets:
            shown = f"{path} -> {os.readlink(path)} (the link only)" if path.is_symlink() else str(path)
            # Escaped: a name with brackets would otherwise be read as markup
            # and listed without them.
            table.add_row(group.name, escape(shown), _format_bytes(size))
            total += size
        table.add_row("[bold]Total[/bold]", "", f"[bold]{_format_bytes(total)}[/bold]")
        console.print(table)
    else:
        console.print("[green]Nothing to do — no durin state found.[/green]")
    if left:
        console.print("Left in place:")
        for path, why in left:
            console.print(f"  {escape(str(path))} — {why}")


def _pip_uninstall_spawn() -> None:
    """Spawn `pip uninstall -y durin-agent` in the background so the parent exits first.

    Targets the PyPI distribution name (``durin-agent``), not the import/CLI
    name ``durin``: ``durin`` isn't an installed distribution, so uninstalling
    it would be a silent no-op that leaves the package on disk.
    """
    cmd = [sys.executable, "-m", "pip", "uninstall", "-y", PYPI_DIST_NAME]
    console.print(f"[dim]Scheduling: {' '.join(cmd)}[/dim]")
    subprocess.Popen(  # noqa: S603 — args are constants, not user input
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _stop_gateway_daemon() -> None:
    """Stop a running gateway daemon so uninstall doesn't orphan it."""
    try:
        from durin.cli.gateway_daemon import daemon_status, stop_daemon

        if daemon_status().state == "running":
            stop_daemon()
            console.print("[green]✓[/green] Stopped the running gateway daemon.")
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]Could not stop the gateway daemon: {e}[/yellow]")


def run_uninstall(
    *,
    assume_yes: bool,
    purge: bool,
    keep_config: bool,
    keep_workspace: bool,
    keep_cache: bool,
    workspace: Path | None = None,
) -> int:
    """Top-level entry; returns a process exit code."""
    from durin.config.home import durin_home as _durin_home_root

    refusal = _refusal(_durin_home_root())
    if refusal:
        console.print(f"[red]Refusing to uninstall:[/red] {escape(refusal)} Nothing was removed.")
        return 1
    try:
        targets = collect_targets(
            keep_config=keep_config,
            keep_workspace=keep_workspace,
            keep_cache=keep_cache,
            workspace=workspace,
        )
        left = left_in_place()
    except OSError as e:
        # Without the home's entries the plan would be incomplete.
        console.print(f"[red]Could not list what to remove: {e}[/red] Nothing was removed.")
        return 1
    _render_plan(targets, left)
    if not targets and not purge:
        return 0
    if not assume_yes:
        confirm_msg = "Delete the paths above?"
        if purge:
            confirm_msg += " (durin package will also be uninstalled)"
        if not typer.confirm(confirm_msg, default=False):
            console.print("[yellow]Aborted.[/yellow]")
            return 1
    # Stop the gateway daemon first — otherwise it survives the uninstall
    # as an orphan still holding its port and PID file.
    _stop_gateway_daemon()
    failures: list[Path] = []
    for _group, path, _size in targets:
        if not _delete(path):
            failures.append(path)
    if failures:
        console.print("[red]Some paths could not be deleted:[/red]")
        for p in failures:
            console.print(f"  - {escape(str(p))}")
    else:
        console.print(f"[green]✓[/green] Removed {len(targets)} path(s).")
    if purge:
        _pip_uninstall_spawn()
        console.print("[green]✓[/green] pip uninstall scheduled.")
    return 0 if not failures else 1


def register(app: typer.Typer) -> None:
    """Attach the `uninstall` command to a Typer app."""

    @app.command("uninstall")
    def uninstall(
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
        purge: bool = typer.Option(False, "--purge", help="Also `pip uninstall durin-agent` afterwards."),
        keep_config: bool = typer.Option(
            False, "--keep-config",
            help="Preserve the config (config.json, config.json.d/, their backups), pairing.json and the "
            "credentials (secrets.json, api_tokens.json, oauth/).",
        ),
        keep_workspace: bool = typer.Option(False, "--keep-workspace", help="Preserve ~/.durin/workspace/."),
        keep_cache: bool = typer.Option(
            False, "--keep-cache",
            help="Preserve the caches: this install's telemetry, the STT and OCR model caches in its home and, "
            "for the default install, ~/.cache/durin/.",
        ),
        workspace: str | None = typer.Option(
            None,
            "--workspace",
            help="Also remove <workspace>/.durin/ scratch (plans, spills, tool-results).",
        ),
    ) -> None:
        """Enumerate durin's on-disk state and (after confirmation) remove it."""
        ws = Path(workspace).expanduser() if workspace else None
        rc = run_uninstall(
            assume_yes=yes,
            purge=purge,
            keep_config=keep_config,
            keep_workspace=keep_workspace,
            keep_cache=keep_cache,
            workspace=ws,
        )
        if rc != 0:
            raise typer.Exit(rc)
