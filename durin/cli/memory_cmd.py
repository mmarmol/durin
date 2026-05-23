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

from datetime import datetime
from pathlib import Path
from typing import Any

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
# G3 helper: parse cursor + entry timestamps as datetime, never string compare
# ---------------------------------------------------------------------------


def _is_at_or_before(entry_ts: str, cursor: Any) -> bool:
    """True iff entry_ts <= cursor in real time (datetime semantics).

    Mirrors :func:`durin.memory.entity_ranker._is_pre_cursor` per G3.
    Numeric cursors (msg_idx) return False — not comparable to ISO ts.
    """
    if not entry_ts or cursor is None:
        return False
    if isinstance(cursor, (int, float)):
        return False
    try:
        et = datetime.fromisoformat(str(entry_ts).replace("Z", "+00:00"))
        ct = datetime.fromisoformat(str(cursor).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return et <= ct


# ---------------------------------------------------------------------------
# dream — manual consolidation trigger
# ---------------------------------------------------------------------------


def _discover_pending_consolidations(
    memory_root: Path,
    *,
    entity_filter: str | None = None,
) -> dict[str, list]:
    """Walk memory/episodic, group entries by entity tag, filter by cursor.

    Returns ``{entity_ref → [EntryRef, ...]}`` sorted by timestamp
    ascending per entity. Pre-cursor entries (those with timestamp at
    or before the entity page's ``dream_processed_through``) are
    excluded — their info is already consolidated.

    G3: cursor comparison uses datetime parsing, not string comparison.
    """
    from durin.memory.dream import EntryRef
    from durin.memory.entity_page import EntityPage
    from durin.memory.storage import load_entry

    pending: dict[str, list] = {}
    episodic_dir = memory_root / "episodic"
    if not episodic_dir.exists():
        return pending

    # Load existing pages to know cursors per entity.
    cursors: dict[str, Any] = {}
    pages_dir = memory_root / "entities"
    if pages_dir.exists():
        for page_path in pages_dir.rglob("*.md"):
            if "/archive/" in str(page_path):
                continue
            page = EntityPage.from_file(page_path)
            if page is None:
                continue
            slug = EntityPage.slug_from_path(page_path)
            ref = f"{page.type}:{slug}"
            if page.dream_processed_through is not None:
                cursors[ref] = page.dream_processed_through

    # Walk episodic entries, group by entity, filter by cursor.
    for entry_path in sorted(episodic_dir.glob("*.md")):
        try:
            entry = load_entry(entry_path)
        except Exception:  # noqa: BLE001
            continue
        ts = entry.valid_from.isoformat() if entry.valid_from else ""
        for ent_ref in entry.entities:
            if entity_filter and ent_ref != entity_filter:
                continue
            if _is_at_or_before(ts, cursors.get(ent_ref)):
                continue  # pre-cursor; already consolidated
            pending.setdefault(ent_ref, []).append(
                EntryRef(
                    id=entry.id,
                    timestamp=ts,
                    text=entry.body,
                    entities=list(entry.entities),
                )
            )

    # Sort each entity's entries by timestamp ascending (oldest first;
    # consolidator caps at MAX_ENTRIES_PER_CALL by taking newest).
    for ref in pending:
        pending[ref].sort(key=lambda e: e.timestamp)
    return pending


@memory_app.command("dream")
def cmd_dream(
    entity: str = typer.Argument(
        None,
        help="Specific entity (e.g. person:marcelo) to consolidate. "
             "If omitted, consolidates all entities with pending entries.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be consolidated without writing.",
    ),
) -> None:
    """Manually trigger memory consolidation (dream pass).

    Walks memory/episodic for entries with entity tags newer than each
    entity page's cursor, groups them by entity, and invokes the LLM
    consolidator. Writes the resulting entity pages + git commits.

    Use ``--dry-run`` to inspect what would be consolidated.
    """
    workspace = _workspace_root()
    memory_root = workspace / "memory"

    if not (memory_root / "episodic").exists():
        console.print("[yellow]No episodic memory yet — nothing to dream.[/yellow]")
        return

    pending = _discover_pending_consolidations(memory_root, entity_filter=entity)
    if not pending:
        if entity:
            console.print(f"[green]No pending consolidations for {entity}.[/green]")
        else:
            console.print("[green]No pending consolidations.[/green]")
        return

    if dry_run:
        console.print("[bold]Dry run — would consolidate:[/bold]\n")
        for ent_ref, entries in pending.items():
            console.print(f"  [cyan]{ent_ref}[/cyan]: {len(entries)} entries")
            for er in entries[:3]:
                preview = er.text[:80].replace("\n", " ")
                console.print(f"    - {er.id}: {preview}")
            if len(entries) > 3:
                console.print(f"    ... +{len(entries) - 3} more")
        return

    # Real consolidation
    from durin.memory.dream import DreamConsolidator, DreamError
    from durin.memory.vector_index import VectorIndex, vector_index_available

    cfg = load_config()

    # W3 (doc 24): pass a VectorIndex so the dream's apply() upserts the
    # consolidated entity_page into the index. Without this, entity pages
    # exist on disk but never enter LanceDB and memory_search can't find
    # them. Best-effort: if memory.enabled=false or fastembed missing,
    # fall through to dream without indexing (markdown remains source of
    # truth).
    vi: VectorIndex | None = None
    try:
        if cfg.memory.enabled and vector_index_available():
            from durin.memory.embedding import FastembedProvider

            provider = FastembedProvider(model=cfg.memory.embedding.model)
            vi = VectorIndex(workspace, provider)
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[yellow]vector index unavailable ({exc}); "
            "entity pages will not be indexed[/yellow]"
        )

    consolidator = DreamConsolidator(workspace=workspace, vector_index=vi)

    for ent_ref, entries in pending.items():
        console.print(f"\n[bold]{ent_ref}[/bold]: {len(entries)} entries")
        try:
            result = consolidator.consolidate_entity(ent_ref, entries)
            sha = consolidator.apply(ent_ref, result)
            if sha:
                console.print(f"  [green]✓[/green] Consolidated → {sha[:8]}")
            else:
                console.print(f"  [dim]= No changes (idempotent)[/dim]")
        except DreamError as exc:
            console.print(f"  [red]✗[/red] {exc}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]✗[/red] unexpected: {exc}")


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
