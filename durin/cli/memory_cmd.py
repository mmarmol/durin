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
    return cfg.workspace_path


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

    # Real consolidation routes through DreamRunner (doc 25 §2.A.1 β.2)
    # so manual runs share the same lock + telemetry surface as the
    # auto-triggers — prevents a user `durin memory dream` from racing
    # the cron tick that fires at 3am. Throttle is disabled for the
    # manual path: the user explicitly asked, respect that.
    from durin.memory.dream_runner import DreamRunner
    from durin.memory.vector_index import VectorIndex, vector_index_available

    cfg = load_config()

    # W3 (doc 24): pass a VectorIndex so dream.apply() upserts the
    # consolidated entity_page into LanceDB. Best-effort: if
    # memory.enabled=false or fastembed missing, fall through without
    # indexing (markdown remains source of truth).
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

    runner = DreamRunner(
        workspace=workspace,
        min_seconds_between_runs=0,
        model=cfg.memory.dream.model_override,
        vector_index=vi,
        # §2.D: opt-in auto-absorb post-dream. Manual `durin memory dream`
        # respects the same config as the auto-triggers — if the user
        # has it enabled, manual runs also auto-merge alias-overlap
        # candidates above threshold.
        auto_absorb_enabled=cfg.memory.dream.auto_absorb.enabled,
        auto_absorb_threshold=cfg.memory.dream.auto_absorb.confidence_threshold,
        auto_absorb_min_age_hours=cfg.memory.dream.auto_absorb.min_age_hours,
        auto_absorb_judge_model=cfg.memory.dream.auto_absorb.judge_model,
    )

    def _on_progress(ent_ref: str, msg: str) -> None:
        console.print(f"  [bold]{ent_ref}[/bold] {msg}")

    result = runner.run(
        trigger="manual",
        entity_filter=entity,
        on_progress=_on_progress,
    )
    if result.ran:
        ok = result.entities_consolidated
        bad = result.entities_failed
        console.print(
            f"\n[green]✓[/green] Consolidated {ok} entit{'y' if ok == 1 else 'ies'} "
            f"in {result.duration_s:.1f}s"
        )
        if bad:
            console.print(f"[red]✗[/red] {bad} failed (see logs)")
    elif result.reason == "concurrent_lock":
        console.print(
            "[yellow]Another dream pass is already running "
            f"({(workspace / 'memory' / '.dream.lock').name}); skipped.[/yellow]"
        )
    elif result.reason == "no_pending":
        # Should not happen — we filtered above — but cover the race
        # where another process consumed everything between checks.
        console.print("[green]No pending consolidations (just absorbed).[/green]")
    else:
        console.print(f"[yellow]Skipped: {result.reason}[/yellow]")


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
    """Revert a memory consolidation. Creates a new commit, doesn't lose history.

    Runs ``git revert --no-edit <sha>`` inside ``memory/.git`` via
    subprocess (dulwich has no porcelain ``revert``; the system ``git``
    binary is required for editable installs anyway and ``durin
    doctor`` warns when it's missing).

    When the target commit was an auto-absorb (``Reason: auto`` trailer)
    a :class:`MemoryAbsorbRevertedEvent` is emitted so doc 25 §2.E
    aggregator can track the regret rate — the only real-world signal
    for tuning ``memory.dream.auto_absorb.confidence_threshold``.
    """
    import subprocess

    from durin.agent.tools._telemetry import emit_tool_event

    repo = _open_repo()
    workspace = _workspace_root()
    memory_root = workspace / "memory"

    commits = repo.log(max_count=200)
    target = next((c for c in commits if c.sha.startswith(commit)), None)
    if target is None:
        console.print(
            f"[red]Commit {commit} not found in last 200 history entries[/red]"
        )
        raise typer.Exit(code=1)

    is_auto_absorb = "auto" in [
        v.strip() for v in target.trailers.get("Reason", [])
    ]
    label = "auto-absorb" if is_auto_absorb else "consolidation"

    if not yes:
        ok = typer.confirm(
            f"Revert {label} {target.sha[:8]} ({target.subject})? "
            f"This creates a new commit that undoes it."
        )
        if not ok:
            console.print("[yellow]Cancelled[/yellow]")
            raise typer.Exit(code=1)

    # Run git revert via subprocess. memory/ is its own repo, so the
    # subprocess runs scoped to that directory. Pass the durin-dream
    # identity via env vars so the command works even when the system
    # git has no user.name / user.email configured (CI runners,
    # freshly-provisioned machines). Matches the convention GitRepo
    # uses for its own commits (durin-dream / dream@durin.local).
    import os as _os

    env = {**_os.environ,
           "GIT_AUTHOR_NAME": "durin-dream",
           "GIT_AUTHOR_EMAIL": "dream@durin.local",
           "GIT_COMMITTER_NAME": "durin-dream",
           "GIT_COMMITTER_EMAIL": "dream@durin.local"}
    try:
        result = subprocess.run(
            ["git", "revert", "--no-edit", target.sha],
            cwd=str(memory_root),
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        console.print(
            "[red]git binary not found in PATH[/red]; install git or run "
            f"`cd {memory_root} && git revert {target.sha}` from a shell with git available."
        )
        raise typer.Exit(code=1) from None

    if result.returncode != 0:
        console.print(f"[red]git revert failed:[/red]\n{result.stderr or result.stdout}")
        raise typer.Exit(code=1)

    console.print(
        f"[green]✓[/green] Reverted {target.sha[:8]} — new commit created."
    )
    if result.stdout.strip():
        console.print(f"[dim]{result.stdout.strip()}[/dim]")

    # §2.D + glm peer review C5: emit reverted event ONLY for auto-absorb
    # targets (manual consolidations don't need the regret signal).
    if is_auto_absorb:
        canonical = (target.trailers.get("Into") or [""])[0].strip()
        absorbed = (target.trailers.get("Absorbed") or [""])[0].strip()
        confidence_raw = (target.trailers.get("Judge-Confidence") or ["0"])[0].strip()
        try:
            confidence = int(confidence_raw)
        except ValueError:
            confidence = 0
        emit_tool_event(
            "memory.absorb.reverted",
            {
                "canonical": canonical,
                "absorbed": absorbed,
                "original_sha": target.sha,
                "confidence": confidence,
            },
        )


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


# ---------------------------------------------------------------------------
# absorb — W4 (doc 24): expose EntityAbsorption via CLI
# ---------------------------------------------------------------------------


def _build_vector_index_optional() -> Any:
    """Construct a VectorIndex if memory.enabled + fastembed available.

    Returns None if disabled or unavailable so callers can pass it through
    to EntityAbsorption (which skips the vector delete step on None).
    """
    from durin.memory.vector_index import VectorIndex, vector_index_available

    cfg = load_config()
    try:
        if cfg.memory.enabled and vector_index_available():
            from durin.memory.embedding import FastembedProvider

            provider = FastembedProvider(model=cfg.memory.embedding.model)
            return VectorIndex(_workspace_root(), provider)
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[yellow]vector index unavailable ({exc}); "
            "absorbed entity row will not be removed from index[/yellow]"
        )
    return None


@memory_app.command("absorb")
def cmd_absorb(
    canonical: str = typer.Argument(
        ...,
        help="Canonical entity ref (the one that survives), e.g. person:marcelo",
    ),
    absorbed: str = typer.Argument(
        ...,
        help="Entity ref to merge into canonical, e.g. person:marcelo-m",
    ),
    reason: str = typer.Option(
        "",
        "--reason",
        "-r",
        help="Free-text reason recorded in the commit trailer.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt.",
    ),
) -> None:
    """Merge two entity pages into one canonical (W4 per doc 24).

    Aliases and identifiers are merged into ``canonical``. The absorbed
    page moves to ``<canonical_slug>/archive/<absorbed_slug>.md`` with
    a frontmatter pointer. Alias index drops the absorbed ref; vector
    index removes its row. A single git commit covers all changes.

    Idempotent: re-running when the absorbed page is already archived
    is a safe no-op.
    """
    from durin.memory.absorption import AbsorptionError, EntityAbsorption

    if ":" not in canonical or ":" not in absorbed:
        raise typer.BadParameter(
            "both args must be '<type>:<slug>' (e.g. person:marcelo)"
        )
    if canonical == absorbed:
        raise typer.BadParameter("canonical and absorbed must differ")

    workspace = _workspace_root()
    if not yes:
        ok = typer.confirm(
            f"Absorb {absorbed} into {canonical}? "
            f"The absorbed page will move to archive."
        )
        if not ok:
            console.print("[yellow]Cancelled[/yellow]")
            raise typer.Exit(code=1)

    vi = _build_vector_index_optional()
    absorber = EntityAbsorption(workspace=workspace, vector_index=vi)
    try:
        sha = absorber.absorb(canonical, absorbed, reason=reason)
    except AbsorptionError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None
    if sha:
        console.print(f"[green]✓[/green] Absorbed {absorbed} → {canonical} ({sha[:8]})")
    else:
        console.print(
            f"[dim]= No-op (absorbed page already archived or nothing to commit)[/dim]"
        )


@memory_app.command("stats")
def cmd_stats(
    days: int = typer.Option(
        None,
        "--days",
        "-d",
        help="Only consider telemetry events from the last N days.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Print the full stats as JSON instead of a rich table.",
    ),
) -> None:
    """Aggregate memory telemetry + filesystem counts.

    Reads JSONL events from ``~/.cache/durin/telemetry/`` and walks the
    workspace's ``memory/`` tree for ground-truth counters. Used to
    measure the gates for the T2 horizon items (see doc 25 §2.E).

    Filesystem counts are point-in-time (what exists now). Event counts
    are over the requested window. Empty workspace + no telemetry yields
    all-zero metrics — no error.
    """
    import json as _json

    from durin.memory.stats import compute_stats

    workspace = _workspace_root()
    stats = compute_stats(workspace, days=days)

    if json_out:
        console.print(_json.dumps(stats.to_dict(), indent=2, default=str))
        return

    # Rich table render — grouped by section so consumers can find the
    # gate-relevant metric at a glance.
    window = f"last {days} days" if days else "all-time"

    fs_table = Table(
        title=f"Filesystem (current snapshot)",
        show_header=True,
        header_style="bold cyan",
    )
    fs_table.add_column("Metric")
    fs_table.add_column("Value", justify="right")
    fs_table.add_row("Episodic entries on disk", str(stats.episodic_entries_on_disk))
    fs_table.add_row(
        "  tagged with entities (gate §2.A)",
        str(stats.episodic_entries_tagged),
    )
    fs_table.add_row("Entity pages on disk", str(stats.entity_pages_on_disk))
    fs_table.add_row("Entity pages archived (post-absorb)",
                     str(stats.entity_pages_archived))
    console.print(fs_table)

    recall_table = Table(
        title=f"Recall events ({window})",
        show_header=True,
        header_style="bold cyan",
    )
    recall_table.add_column("Metric")
    recall_table.add_column("Value", justify="right")
    recall_table.add_row("Total recalls", str(stats.recall_total))
    recall_table.add_row("  vector path", str(stats.recall_vector_total))
    recall_table.add_row("  grep path", str(stats.recall_grep_total))
    recall_table.add_row(
        "Vector entity-aware activations",
        f"{stats.recall_vector_entity_aware} "
        f"({stats.entity_aware_ratio*100:.1f}%)",
    )
    recall_table.add_row(
        "Vector reordered (ranker fired effectively)",
        f"{stats.recall_vector_reordered} "
        f"({stats.reordered_ratio*100:.1f}%)",
    )
    if stats.recall_vector_total > 0:
        avg_ms = (
            stats.recall_vector_duration_ms_total / stats.recall_vector_total
        )
        recall_table.add_row("Avg vector duration", f"{avg_ms:.1f} ms")
        avg_hits = (
            stats.recall_vector_hit_count_total / stats.recall_vector_total
        )
        recall_table.add_row("Avg vector hits/call", f"{avg_hits:.1f}")
    console.print(recall_table)

    write_table = Table(
        title=f"Store + ingest events ({window})",
        show_header=True,
        header_style="bold cyan",
    )
    write_table.add_column("Metric")
    write_table.add_column("Value", justify="right")
    write_table.add_row("Store writes (successful)", str(stats.store_total))
    write_table.add_row(
        "Store blocked as near-duplicate (gate §2.D)",
        str(stats.store_blocked_near_duplicate),
    )
    write_table.add_row("Ingest events", str(stats.ingest_total))
    write_table.add_row("Ingest bytes total", f"{stats.ingest_bytes_total:,}")
    console.print(write_table)

    embed_table = Table(
        title=f"Embedding events ({window})",
        show_header=True,
        header_style="bold cyan",
    )
    embed_table.add_column("Metric")
    embed_table.add_column("Value", justify="right")
    embed_table.add_row("Embedding loads", str(stats.embedding_load_count))
    embed_table.add_row(
        "Embedding load duration total",
        f"{stats.embedding_load_duration_ms_total:.0f} ms",
    )
    embed_table.add_row("Embed batch calls", str(stats.embedding_embed_count))
    embed_table.add_row(
        "Embed batches sum size",
        str(stats.embedding_embed_batch_size_total),
    )
    console.print(embed_table)

    console.print(
        f"\n[dim]Scanned {stats.telemetry_files_scanned} telemetry files, "
        f"{stats.telemetry_events_scanned} memory.* events.[/dim]"
    )


@memory_app.command("absorb-suggest")
def cmd_absorb_suggest() -> None:
    """List candidate pairs that share at least one alias (merge hints)."""
    from durin.memory.absorption import EntityAbsorption

    workspace = _workspace_root()
    absorber = EntityAbsorption(workspace=workspace)
    candidates = absorber.find_candidates()
    if not candidates:
        console.print("[green]No merge candidates — no aliases overlap across pages.[/green]")
        return

    table = Table(title="Merge candidates", show_lines=False)
    table.add_column("Entity A", style="cyan", no_wrap=True)
    table.add_column("Entity B", style="cyan", no_wrap=True)
    table.add_column("Shared aliases", style="yellow")
    for c in candidates:
        a, b = c.refs
        table.add_row(a, b, ", ".join(c.shared_aliases))
    console.print(table)
    console.print(
        "\n[dim]To merge: durin memory absorb <canonical> <absorbed> "
        "--reason <why>[/dim]"
    )
