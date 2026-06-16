"""`durin auth token` — manage persisted API tokens.

Thin CLI over :class:`durin.security.api_tokens.ApiTokenStore`.  Operates
directly on the store (sync, no gateway required).

Subcommands:
  list    — print token metadata (id, label, scopes, created/expires/last_used).
  issue   — mint a new token; prints the plaintext ONCE.
  revoke  — remove a token by id.
"""

from __future__ import annotations

import datetime
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from durin.security.api_tokens import ApiTokenStore
from durin.service.principal import Scope

console = Console()

auth_app = typer.Typer(
    help="Manage API auth tokens (issue, list, revoke).",
    no_args_is_help=True,
)

token_app = typer.Typer(
    help="Manage persisted API tokens.",
    no_args_is_help=True,
)

auth_app.add_typer(token_app, name="token")

_VALID_SCOPES = {s.value for s in Scope}


def _fmt_ts(ts: float | None) -> str:
    """Format a Unix timestamp as a short ISO date-time string, or '—'."""
    if ts is None:
        return "[dim]—[/dim]"
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


@token_app.command("list")
def cmd_list() -> None:
    """List all API tokens — metadata only, never the plaintext or hash."""
    store = ApiTokenStore()
    tokens = store.list_tokens()
    if not tokens:
        console.print("[dim]No tokens issued. Use `durin auth token issue` to create one.[/dim]")
        return
    table = Table(title="API tokens")
    table.add_column("ID", style="bold cyan")
    table.add_column("Label")
    table.add_column("Scopes")
    table.add_column("Created")
    table.add_column("Expires")
    table.add_column("Last used")
    for t in tokens:
        table.add_row(
            t["token_id"],
            t.get("label") or "[dim]—[/dim]",
            ", ".join(t.get("scopes") or []) or "[dim]—[/dim]",
            _fmt_ts(t.get("created_at")),
            _fmt_ts(t.get("expires_at")),
            _fmt_ts(t.get("last_used_at")),
        )
    console.print(table)


@token_app.command("issue")
def cmd_issue(
    scopes: str = typer.Option(
        ...,
        "--scopes",
        "-s",
        help=(
            "Comma-separated list of scopes to grant.  "
            f"Valid values: {', '.join(sorted(_VALID_SCOPES))}."
        ),
    ),
    label: str = typer.Option("", "--label", "-l", help="Human-readable label for this token."),
    ttl: Optional[int] = typer.Option(
        None,
        "--ttl",
        help="Token lifetime in seconds.  Omit for a non-expiring token.",
    ),
) -> None:
    """Issue a new API token.  The plaintext is printed ONCE — store it now."""
    parsed = [s.strip() for s in scopes.split(",") if s.strip()]
    if not parsed:
        console.print("[red]✗[/red] --scopes must not be empty.")
        raise typer.Exit(1)

    unknown = sorted(set(parsed) - _VALID_SCOPES)
    if unknown:
        console.print(
            f"[red]✗[/red] Unknown scope(s): {', '.join(unknown)}\n"
            f"Valid scopes: {', '.join(sorted(_VALID_SCOPES))}"
        )
        raise typer.Exit(1)

    store = ApiTokenStore()
    token_id, plaintext = store.issue(parsed, label=label, ttl_s=float(ttl) if ttl is not None else None)

    console.print()
    console.print("[green]✓[/green] Token issued.")
    console.print(f"  [bold]ID:[/bold]     {token_id}")
    console.print(f"  [bold]Scopes:[/bold] {', '.join(parsed)}")
    if label:
        console.print(f"  [bold]Label:[/bold]  {label}")
    if ttl is not None:
        console.print(f"  [bold]TTL:[/bold]    {ttl}s")
    else:
        console.print("  [bold]TTL:[/bold]    non-expiring")
    console.print()
    console.print("[yellow bold]Token (store this now — it will NOT be shown again):[/yellow bold]")
    console.print(f"  {plaintext}")
    console.print()


@token_app.command("revoke")
def cmd_revoke(
    token_id: str = typer.Argument(..., help="Token ID to revoke (from `durin auth token list`)."),
) -> None:
    """Revoke an API token by its ID."""
    store = ApiTokenStore()
    if store.revoke(token_id):
        console.print(f"[green]✓[/green] Token [bold]{token_id}[/bold] revoked.")
    else:
        console.print(f"[red]✗[/red] No token with ID '{token_id}'.")
        raise typer.Exit(1)
