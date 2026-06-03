"""`durin skill` — inspect and audit skills.

Subcommands:

- ``durin skill audit <name|path>`` — run the §8.C security scan on an
  existing skill: the format lint (:func:`validate_skill`) plus the
  deterministic security scan (:func:`scan_skill`), rendering a verdict
  (safe/caution/dangerous) and a table of findings.
- ``durin skill list`` — table of active skills (name, source, mode, verdict)
  from the Skills-Surface inventory.
- ``durin skill quarantine`` — table of skills awaiting an import decision
  (name, source, verdict) plus each entry's findings.
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


@skill_app.command("search")
def cmd_search(
    query: str = typer.Argument(..., help="What to search for across the configured skill registries."),
    limit: int = typer.Option(0, "--limit", "-n", help="Max results (0 = config default)."),
) -> None:
    """Search external skill registries (skills.sh, …) for a skill.

    Prints ranked hits; import one through the agent's `skill_import` tool or the
    web panel — `search` never installs.
    """
    import asyncio

    from durin.agent.skill_registry import build_adapters, search_registries

    cfg = load_config()
    disc = cfg.skills.discovery
    hits = asyncio.run(search_registries(
        query,
        adapters=build_adapters(disc.registries),
        allowlist=list(cfg.skills.security.allowlist),
        limit=limit or disc.search_limit,
    ))
    if not hits:
        console.print(f"[dim]No skills found for[/dim] {query!r}.")
        return
    table = Table(title=f"Skill search: {query}", show_lines=False)
    table.add_column("Name", style="bold", no_wrap=True)
    table.add_column("Registry", style="cyan", no_wrap=True)
    table.add_column("Installs", justify="right", no_wrap=True)
    table.add_column("Ref", style="dim", overflow="fold")
    for h in hits:
        installs = h.signals.get("installs")
        table.add_row(h.name, h.registry,
                      f"{installs:,}" if isinstance(installs, int) else "-", h.ref)
    console.print(table)


def _verdict_cell(verdict: str) -> str:
    style = _VERDICT_STYLE.get(verdict, "white")
    return f"[{style}]{verdict or '-'}[/{style}]"


@skill_app.command("list")
def list_skills() -> None:
    """List active skills with their §8.C verdict.

    Renders the Skills-Surface inventory (name, source, mode, verdict) for the
    skills available in the current workspace.
    """
    from durin.agent.skills_surface import skills_inventory

    inv = skills_inventory(_workspace_root())
    if not inv:
        console.print("[dim]No skills installed.[/dim]")
        return

    table = Table(title="Skills", show_lines=False)
    table.add_column("Name", style="bold", no_wrap=True)
    table.add_column("Source", style="cyan", no_wrap=True)
    table.add_column("Mode", no_wrap=True)
    table.add_column("Verdict", no_wrap=True)
    for s in inv:
        table.add_row(
            s["name"],
            s.get("source", ""),
            s.get("mode", ""),
            _verdict_cell(s.get("verdict", "")),
        )
    console.print(table)


@skill_app.command("quarantine")
def cmd_quarantine() -> None:
    """List skills awaiting an import decision (.durin/import-quarantine).

    Renders each pending entry (name, source, verdict) plus its findings; prints
    a friendly message when quarantine is empty.
    """
    from durin.agent.skills_surface import quarantined_skills

    pending = quarantined_skills(_workspace_root())
    if not pending:
        console.print("[green]No skills in quarantine.[/green]")
        return

    table = Table(title="Quarantined skills", show_lines=True)
    table.add_column("Name", style="bold", no_wrap=True)
    table.add_column("Source", style="cyan", no_wrap=True)
    table.add_column("Verdict", no_wrap=True)
    table.add_column("Findings")
    for s in pending:
        findings = s.get("findings", [])
        if findings:
            detail = "\n".join(
                f"{f.get('category', '?')} ({f.get('where', '?')})" for f in findings
            )
        else:
            detail = "[dim]none[/dim]"
        table.add_row(
            s["name"],
            s.get("source", ""),
            _verdict_cell(s.get("verdict", "")),
            detail,
        )
    console.print(table)
