"""`durin memory` subcommand: drill-down + introspection on entity pages.

Wraps :class:`GitRepo`
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
    from durin.utils.git_repo import GitRepo

    workspace = _workspace_root()
    repo = GitRepo(workspace / "memory")
    if not repo.is_initialized():
        console.print(
            "[yellow]memory/.git/ has not been initialized yet[/yellow] — "
            "no consolidations have run. Try a dream pass first."
        )
        raise typer.Exit(code=1)
    return repo


# Exposed for the health-check recovery-hint anti-drift test
# (tests/memory/test_health_critical_a7_recovery_hint.py). If this
# tuple changes, the `_RECOVERY_HINTS` dict in
# `durin/memory/health_check.py` must be updated or the test fails.
VALID_REINDEX_TARGETS: tuple[str, ...] = ("all", "fts", "lancedb")


@memory_app.command("reindex")
def cmd_reindex(
    target: str = typer.Option(
        "all",
        "--target",
        help="Which index to rebuild: 'fts' | 'lancedb' | 'all' (default).",
    ),
) -> None:
    """Wipe `.durin/index/` and rebuild from `memory/`.

    Markdown is the source of truth. After a corruption / migration /
    suspected staleness, this command rebuilds both the FTS5 lexical
    tables and (when available) the LanceDB vector table from scratch.

    The walk respects `walk_memory(workspace)` — `memory/archive/**`
    and `memory/pending/**` are excluded, same rule as production.
    """
    from durin.memory.indexer import rebuild_fts_index

    workspace = _workspace_root()
    if not (workspace / "memory").is_dir():
        console.print(
            f"[yellow]No memory/ directory at {workspace} — nothing to "
            f"index.[/yellow]"
        )
        raise typer.Exit(code=0)

    target = target.lower()
    if target not in VALID_REINDEX_TARGETS:
        raise typer.BadParameter(
            f"--target must be one of {VALID_REINDEX_TARGETS} "
            f"(got {target!r})"
        )

    if target in ("all", "fts"):
        console.print("[bold]Rebuilding FTS5 lexical index…[/bold]")
        stats = rebuild_fts_index(workspace)
        console.print(
            f"  Indexed: [green]{stats.indexed}[/green]  "
            f"Errors: "
            f"[{'red' if stats.errors else 'green'}]{stats.errors}[/]"
        )

    if target in ("all", "lancedb"):
        try:
            from durin.memory.vector_index import (
                VectorIndex,
                vector_index_available,
            )
        except ImportError:
            console.print(
                "[yellow]LanceDB optional dependency missing; "
                "skipping vector rebuild. "
                "Install with `pip install durin[memory]`.[/yellow]"
            )
        else:
            if not vector_index_available():
                console.print(
                    "[yellow]LanceDB not available; skipping vector "
                    "rebuild.[/yellow]"
                )
            else:
                console.print(
                    "[bold]Rebuilding LanceDB vector index…[/bold]"
                )
                # Vector rebuild uses the CONFIGURED embedding model, and
                # records it in meta.json (N5a) so ensure_index_fresh can detect
                # a later model swap.
                try:
                    from durin.config.loader import load_config
                    from durin.memory.embedding import FastembedProvider
                    from durin.memory.index_meta import record_built_model
                    model = load_config().memory.embedding.model
                    provider = FastembedProvider(model=model)
                    vi = VectorIndex(workspace, provider)
                    count = vi.rebuild_from_workspace()
                    record_built_model(workspace, model)
                    console.print(
                        f"  Indexed: [green]{count}[/green] rows "
                        f"(model: {model})"
                    )
                except Exception as exc:  # noqa: BLE001
                    console.print(
                        f"[red]Vector rebuild failed:[/red] {exc}"
                    )
                    raise typer.Exit(code=1) from exc

    console.print("[green]Reindex complete.[/green]")


@memory_app.command("dream")
def cmd_dream(
    entity: str = typer.Argument(
        None,
        help="Specific entity (e.g. person:marcelo). Note: the current dream "
             "passes process all recent sessions; per-entity filtering is not "
             "yet applied.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be consolidated without writing.",
    ),
) -> None:
    """Manually trigger the dream passes.

    Runs the extract pass (reads each session's new turns and extracts entity
    attributes) followed by the refine pass (dedups duplicate entities). Writes
    entity pages via the memory writer (git-committed).
    """
    workspace = _workspace_root()
    # The manual dream runs the extract pass (sessions → entity attributes)
    # + the refine pass (dedup). The `entity` filter is not used by the
    # new passes.
    from durin.memory.always_on_dream import run_always_on_pass
    from durin.memory.dream_passes import (
        dream_vector_index,
        run_derived_from_pass,
        run_extract_pass,
        run_refine_pass,
        run_skill_extract_pass,
    )
    from durin.memory.model_resolve import resolve_memory_model

    if dry_run:
        console.print(
            "[yellow]--dry-run is not supported by the new dream passes; "
            "run without it to extract + refine.[/yellow]"
        )
        return

    cfg = load_config()
    model = resolve_memory_model(cfg)
    from datetime import datetime, timezone
    _run_started = datetime.now(timezone.utc)
    _vi = dream_vector_index(workspace, cfg)
    _absorb = cfg.memory.dream.auto_absorb
    console.print("[dim]Extract pass (sessions → entity attributes)…[/dim]")
    ex = run_extract_pass(workspace, model=model,
                          discover=cfg.memory.dream.discover_enabled,
                          confidence_threshold=_absorb.confidence_threshold,
                          semantic_distance_threshold=_absorb.semantic_distance_threshold,
                          vector_index=_vi)
    console.print("[dim]Derived-from pass (link entities → source documents)…[/dim]")
    df = run_derived_from_pass(workspace, model=model)
    console.print("[dim]Skill-extract pass (sessions → reusable procedures)…[/dim]")
    sk = run_skill_extract_pass(workspace, model=model)
    if _absorb.enabled:
        console.print("[dim]Refine pass (dedup duplicate entities)…[/dim]")
    else:
        console.print(
            "[dim]Refine pass skipped — auto_absorb disabled "
            "(use 'durin memory absorb-suggest' to review duplicates)[/dim]"
        )
    rf = run_refine_pass(workspace, model=model, enabled=_absorb.enabled,
                         confidence_threshold=_absorb.confidence_threshold,
                         escalate_floor=_absorb.escalate_floor,
                         semantic_distance_threshold=_absorb.semantic_distance_threshold,
                         run_started_at=_run_started,
                         vector_index=_vi)
    console.print("[dim]Always-on pass (distil pinned guidance)…[/dim]")
    ao = run_always_on_pass(workspace, model=model,
                            token_budget=cfg.memory.dream.always_on_token_budget)
    merged = len(rf.get("merged", []))
    console.print(
        f"\n[green]✓[/green] extract: {ex['entities']} attribute update(s) across "
        f"{ex['sessions']} session(s); "
        f"derived_from: {df.get('links', 0)} link(s); "
        f"skills: {sk.get('skills_touched', 0)}; "
        f"refine: {merged} merge(s); "
        f"always_on: {ao.get('selected', 0)} pinned ({ao.get('tokens', 0)} tok)"
    )
    if ex.get("errors"):
        console.print(
            f"[yellow]{len(ex['errors'])} session(s) errored (see logs)[/yellow]"
        )


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
    a :class:`MemoryAbsorbRevertedEvent` is emitted so the regret-rate
    aggregator can track merges — the only real-world signal for tuning
    ``memory.dream.auto_absorb.confidence_threshold``.
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

    # Emit reverted event ONLY for auto-absorb targets (manual
    # consolidations don't need the regret signal).
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
            "[dim]= No-op (absorbed page already archived or nothing to commit)[/dim]"
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
    measure long-term memory health trends.

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
        title="Filesystem (current snapshot)",
        show_header=True,
        header_style="bold cyan",
    )
    fs_table.add_column("Metric")
    fs_table.add_column("Value", justify="right")
    fs_table.add_row("Episodic entries on disk", str(stats.episodic_entries_on_disk))
    fs_table.add_row(
        "  tagged with entities",
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
    recall_table.add_row("  skill recalls", str(stats.recall_skill_total))
    recall_table.add_row("Skill misses", str(stats.skill_miss_total))
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
        "Store blocked as near-duplicate",
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



# ---------------------------------------------------------------------------
# forget — archive an individual memory entry (matches VAULT_README promise)
# ---------------------------------------------------------------------------

@memory_app.command("forget")
def cmd_forget(
    uri: str = typer.Argument(
        ...,
        help="Entry URI in 'memory/<class>/<id>' form (the same shape memory_search returns).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation prompt.",
    ),
) -> None:
    """Archive a single memory entry: moves it to ``memory/archive/<class>/<id>.md``
    and removes its vector + FTS index rows.

    Refuses to forget ``memory/entities/...`` URIs — entity pages have
    their own absorb/revert lifecycle (see ``durin memory absorb`` /
    ``durin memory revert``). Use those instead.
    """
    from durin.memory.forget import (
        FORGETTABLE_CLASSES,
        ForgetError,
        forget_entry,
        parse_memory_uri,
    )

    try:
        class_name, entry_id = parse_memory_uri(uri)
    except ForgetError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None
    workspace = _workspace_root()

    if class_name == "entities":
        console.print(
            "[red]Refusing to forget entity pages.[/red] "
            "Entities have their own lifecycle: use "
            "[bold]durin memory absorb[/bold] (merge) or "
            "[bold]durin memory revert[/bold] (undo a consolidation)."
        )
        raise typer.Exit(code=2)

    if class_name not in FORGETTABLE_CLASSES:
        console.print(
            f"[red]Unsupported class '{class_name}'.[/red] "
            f"Supported: {', '.join(FORGETTABLE_CLASSES)}."
        )
        raise typer.Exit(code=2)

    entry_path = workspace / "memory" / class_name / f"{entry_id}.md"
    if not entry_path.is_file():
        console.print(f"[red]Entry not found:[/red] {entry_path}")
        raise typer.Exit(code=1)

    if not yes:
        confirm = typer.confirm(
            f"Archive {uri} → memory/archive/{class_name}/{entry_id}.md?",
            default=False,
        )
        if not confirm:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=1)

    # Archive + index cleanup live in the shared forget_entry helper so
    # the CLI and the agent's `memory_forget` tool stay index-consistent.
    try:
        forget_entry(workspace, uri, reason="user_forget")
    except ForgetError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Forget failed:[/red] {exc}")
        raise typer.Exit(code=1) from None

    console.print(
        f"[green]Forgot[/green] {uri} "
        f"→ memory/archive/{class_name}/{entry_id}.md"
    )
