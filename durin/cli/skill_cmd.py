"""`durin skill` — inspect and audit skills.

Subcommands:

- ``durin skill audit <name|path>`` — run the §8.C security scan on an
  existing skill: the format lint (:func:`validate_skill`) plus the
  deterministic security scan (:func:`scan_skill`), rendering a verdict
  (safe/caution/dangerous) and a table of findings.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from durin.config.loader import load_config

console = Console()

skill_app = typer.Typer(
    name="skill",
    help="Inspect and audit skills.",
    no_args_is_help=True,
)

_VERDICT_STYLE = {"safe": "green", "caution": "yellow", "dangerous": "red"}


@skill_app.callback()
def _skill_root() -> None:
    """Inspect and audit skills."""


def _workspace_root() -> Path:
    cfg = load_config()
    return cfg.workspace_path


def _resolve_skill_dir(target: str) -> Path:
    """Map ``<name>`` → workspace ``skills/<name>``; treat anything that looks
    like a path (or an existing dir) as a path."""
    candidate = Path(target).expanduser()
    if candidate.is_dir() and (("/" in target) or ("\\" in target)):
        return candidate
    return _workspace_root() / "skills" / target


@skill_app.command("audit")
def cmd_audit(
    target: str = typer.Argument(
        ...,
        help="Skill name (under the workspace 'skills/') or a path to a skill dir.",
    ),
) -> None:
    """Run the security scan on an existing skill and print the verdict.

    Resolves ``target`` to a skill directory (a workspace ``skills/<name>``
    or a path), runs the format lint plus the deterministic security scan,
    and renders the verdict + findings.
    """
    from durin.agent.skills_import import validate_skill
    from durin.security.skill_scan import scan_skill

    skill_dir = _resolve_skill_dir(target)
    if not skill_dir.is_dir():
        console.print(f"[red]Skill not found:[/red] {target}")
        raise typer.Exit(code=1)

    rep = validate_skill(skill_dir)
    scan = scan_skill(skill_dir)

    style = _VERDICT_STYLE.get(scan.verdict, "white")
    console.print(
        f"[bold]{rep.name}[/bold] — verdict: [{style}]{scan.verdict}[/{style}]"
        f"  (carries_code: {rep.carries_code})"
    )

    if scan.findings:
        table = Table(title="Findings", show_lines=False)
        table.add_column("Severity", no_wrap=True)
        table.add_column("Category", style="cyan", no_wrap=True)
        table.add_column("Where", style="dim", no_wrap=True)
        table.add_column("Detail")
        for f in scan.findings:
            sev_style = _VERDICT_STYLE.get(f.severity, "white")
            table.add_row(
                f"[{sev_style}]{f.severity}[/{sev_style}]",
                f.category,
                f.where,
                f.detail,
            )
        console.print(table)
    else:
        console.print("[green]No security findings.[/green]")

    if rep.errors:
        console.print("[red]Lint errors:[/red] " + "; ".join(rep.errors))
    if rep.warnings:
        console.print("[yellow]Lint warnings:[/yellow] " + "; ".join(rep.warnings))
