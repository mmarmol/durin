"""`durin memory` subcommand: drill-down + introspection on entity pages.

Phase 4 per ``docs/19_implementation_plan.md`` §6. Wraps :class:`GitRepo`
over ``memory/.git/`` so the user can navigate the entity-centric memory
without invoking git directly. The drill-down operation (``expand``)
traverses sources, related entities, and prior versions to surface why
a page looks the way it does.

Subcommands:

- ``durin memory history <entity>`` — chronological diff overview
- ``durin memory diff <entity> [from..to]`` — unified diff
- ``durin memory show <entity> [rev]`` — page contents at a revision
- ``durin memory revert <commit>`` — undo a consolidation
- ``durin memory expand <entity>`` — sources + related + archived
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from durin.config.loader import load_config

console = Console()

memory_app = typer.Typer(
    name="memory",
    help="Inspect and navigate the entity-centric memory (see docs/18).",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workspace_root() -> Path:
    cfg = load_config()
    return cfg.workspace_path()


def _resolve_entity_path(entity_ref: str) -> Path:
    """Map ``person:marcelo`` → ``memory/entities/person/marcelo.md`` in workspace."""
    if ":" not in entity_ref:
        raise typer.BadParameter(
            f"entity must be '<type>:<slug>' (e.g. person:marcelo), got: {entity_ref}"
        )
    type_, slug = entity_ref.split(":", 1)
    workspace = _workspace_root()
    return workspace / "memory" / "entities" / type_ / f"{slug}.md"


def _open_repo():
    """Construct a :class:`GitRepo` rooted at ``memory/`` if it exists."""
    from durin.utils.git_repo import GitRepo, GitRepoError

    workspace = _workspace_root()
    repo = GitRepo(workspace / "memory")
    if not repo.is_initialized():
        console.print(
            "[yellow]memory/.git/ has not been initialized yet[/yellow] — "
            "no consolidations have run. Try a dream pass first."
        )
        raise typer.Exit(code=1)
    return repo


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@memory_app.command("history")
def cmd_history(
    entity: str = typer.Argument(
        ...,
        help="Entity ref, e.g. person:marcelo",
        metavar="ENTITY",
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Max commits to show"),
) -> None:
    """Show the consolidation history for an entity page."""
    repo = _open_repo()
    page_path = _resolve_entity_path(entity)
    commits = repo.log(page_path, max_count=limit)

    if not commits:
        console.print(f"[yellow]No history yet for {entity}[/yellow]")
        return

    table = Table(title=f"History for {entity}", show_lines=False)
    table.add_column("Rev", style="cyan", no_wrap=True)
    table.add_column("When", style="green", no_wrap=True)
    table.add_column("Subject")
    table.add_column("Trailers", style="dim")
    for c in commits:
        sources = ", ".join(c.trailers.get("Sources", []))
        entities = ", ".join(c.trailers.get("Entities-touched", []))
        trailer_text = f"sources: {sources}\nentities: {entities}" if sources or entities else ""
        table.add_row(
            c.sha[:8],
            c.timestamp.strftime("%Y-%m-%d %H:%M"),
            c.subject,
            trailer_text,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@memory_app.command("show")
def cmd_show(
    entity: str = typer.Argument(..., help="Entity ref, e.g. person:marcelo"),
    rev: str = typer.Option(
        "HEAD",
        "--rev",
        "-r",
        help="Git revision (defaults to HEAD = current page). Use 'history' first to find SHAs.",
    ),
) -> None:
    """Print the entity page's markdown content at a given revision."""
    repo = _open_repo()
    page_path = _resolve_entity_path(entity)
    if rev == "HEAD":
        # Just read the current file from disk.
        if not page_path.exists():
            console.print(f"[red]No page on disk for {entity}[/red]")
            raise typer.Exit(code=1)
        # markup=False so YAML lists like ``aliases: [a, b]`` aren't
        # parsed as rich style tags and silently stripped.
        console.print(page_path.read_text(encoding="utf-8"), markup=False)
        return
    # Lookup at a specific commit
    try:
        text = repo.show(rev, page_path)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not show {entity} at {rev}: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(text, markup=False)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


@memory_app.command("diff")
def cmd_diff(
    entity: str = typer.Argument(..., help="Entity ref"),
    revs: str = typer.Argument(
        ...,
        help="Two revisions joined by '..' (e.g. abc1234..def5678)",
    ),
) -> None:
    """Show the diff for an entity page between two revisions."""
    if ".." not in revs:
        raise typer.BadParameter("revs must be '<from>..<to>' (e.g. abc..def)")
    from_sha, _, to_sha = revs.partition("..")
    repo = _open_repo()
    page_path = _resolve_entity_path(entity)
    try:
        diff = repo.diff(from_sha, to_sha, page_path)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Diff failed: {exc}[/red]")
        raise typer.Exit(code=1) from None
    if not diff.strip():
        console.print(f"[dim]No changes for {entity} between {from_sha[:8]} and {to_sha[:8]}[/dim]")
        return
    console.print(diff)


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------


@memory_app.command("revert")
def cmd_revert(
    commit: str = typer.Argument(..., help="Commit SHA to revert"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt.",
    ),
) -> None:
    """Revert a memory consolidation. Creates a new commit, doesn't lose history."""
    repo = _open_repo()
    if not yes:
        ok = typer.confirm(
            f"Revert commit {commit[:8]}? This creates a new commit that undoes it."
        )
        if not ok:
            console.print("[yellow]Cancelled[/yellow]")
            raise typer.Exit(code=1)

    # dulwich doesn't have a porcelain `revert` we can lean on directly,
    # so build the reverse change manually: re-apply the parent content
    # of the target file via a new commit.
    try:
        from durin.utils.git_repo import GitRepo

        commits = repo.log(max_count=50)
        target = next((c for c in commits if c.sha.startswith(commit)), None)
        if target is None:
            console.print(f"[red]Commit {commit} not found in last 50 history[/red]")
            raise typer.Exit(code=1)
        # For Phase 4 simplicity: print guidance instead of mutating.
        # Full revert requires reverse-applying the diff; users with
        # critical needs can `git revert <sha>` directly inside memory/.
        console.print(
            f"[yellow]revert is partially implemented in v1[/yellow]: "
            f"run `cd ~/.durin/workspace/memory && git revert {commit}` "
            f"to undo the consolidation safely."
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Revert preparation failed: {exc}[/red]")
        raise typer.Exit(code=1) from None


# ---------------------------------------------------------------------------
# expand — drill-down
# ---------------------------------------------------------------------------


@memory_app.command("expand")
def cmd_expand(
    entity: str = typer.Argument(..., help="Entity ref to expand"),
) -> None:
    """Drill-down: show sources, related entities, archived absorptions.

    The page's own content + git history + alias_index + archive folder
    are surfaced in one view so the user (or future tooling) can
    navigate from a single result toward its full context.
    """
    repo = _open_repo()
    page_path = _resolve_entity_path(entity)
    if not page_path.exists():
        console.print(f"[red]No page on disk for {entity}[/red]")
        raise typer.Exit(code=1)

    from durin.memory.entity_page import EntityPage

    page = EntityPage.from_file(page_path)
    if page is None:
        console.print(f"[red]Page at {page_path} could not be parsed[/red]")
        raise typer.Exit(code=1)

    # Section 1: current page summary
    console.print(f"[bold cyan]{entity}[/bold cyan]")
    console.print(f"  name: {page.name}", markup=False)
    if page.aliases:
        console.print(f"  aliases: {', '.join(page.aliases)}", markup=False)
    if page.extra:
        for key, value in page.extra.items():
            console.print(f"  {key}: {value}", markup=False)
    console.print()

    # Section 2: history
    commits = repo.log(page_path, max_count=10)
    if commits:
        console.print(f"[bold]History ({len(commits)} revisions)[/bold]")
        for c in commits:
            console.print(
                f"  [cyan]{c.sha[:8]}[/cyan] "
                f"[green]{c.timestamp.strftime('%Y-%m-%d')}[/green] "
                f"{c.subject}"
            )
        console.print()

    # Section 3: sources mentioned in latest commit
    if commits:
        latest = commits[0]
        sources = latest.trailers.get("Sources", [])
        if sources:
            console.print("[bold]Sources (latest revision)[/bold]")
            for src in sources:
                console.print(f"  • {src}")
            console.print()
        related = latest.trailers.get("Entities-referenced", [])
        if related:
            console.print("[bold]Related entities[/bold]")
            for ent in related:
                console.print(f"  • {ent}")
            console.print()

    # Section 4: archive subfolder
    type_, slug = entity.split(":", 1)
    workspace = _workspace_root()
    archive_dir = workspace / "memory" / "entities" / type_ / slug / "archive"
    if archive_dir.exists():
        archived = sorted(archive_dir.glob("*.md"))
        if archived:
            console.print(f"[bold]Archived absorptions ({len(archived)})[/bold]")
            for path in archived:
                console.print(f"  • {path.relative_to(workspace)}")
            console.print()
