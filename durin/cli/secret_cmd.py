"""`durin secret` — manage the secret store.

Thin CLI over :class:`durin.security.secrets.SecretStore`. Values are
entered through a hidden prompt and never printed back (``list`` and
``show`` mask them; ``show --reveal`` is the one explicit exception).

"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from durin.security.secrets import SecretStore, is_valid_secret_name

console = Console()

secret_app = typer.Typer(
    help="Manage stored secrets (API keys, tokens) — see `docs/11_secrets_design.md`.",
    no_args_is_help=True,
)


def _mask(value: str) -> str:
    """Mask a secret value for display — last 4 chars at most."""
    if len(value) <= 4:
        return "••••"
    return "••••" + value[-4:]


def _parse_scope(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


@secret_app.command("set")
def cmd_set(
    name: str = typer.Argument(..., help="Secret name (UPPER_SNAKE, env-var-safe)."),
    service: str | None = typer.Option(
        None, "--service", "-s",
        help="What the secret is for, e.g. 'atlassian' or 'provider:openai'. "
        "Omit to rotate an existing secret's value (metadata preserved).",
    ),
    account: str | None = typer.Option(
        None, "--account", "-a", help="Distinguisher within a service (e.g. 'work')."
    ),
    description: str = typer.Option("", "--description", "-d", help="Human description."),
    scope: str | None = typer.Option(
        None, "--scope",
        help="Comma-separated consumer tags: exec, skill:*, channel:telegram, …",
    ),
) -> None:
    """Store a secret. The value is read from a hidden prompt."""
    if not is_valid_secret_name(name):
        console.print(
            f"[red]✗[/red] Invalid name '{name}' — use UPPER_SNAKE "
            "(matches [A-Z][A-Z0-9_]*)."
        )
        raise typer.Exit(1)
    existed = SecretStore().load().get(name) is not None
    if service is None and not existed:
        console.print(
            f"[red]✗[/red] Secret [bold]{name}[/bold] does not exist — "
            "pass --service to create it."
        )
        raise typer.Exit(1)
    value = typer.prompt(f"Value for {name}", hide_input=True)
    if not value:
        console.print("[yellow]Empty value — nothing stored.[/yellow]")
        raise typer.Exit(1)
    # The write goes through the service layer's single source of truth
    # (validation + put/save/reload) — same path as the webui, the websocket
    # need-secret frame, and the TUI prompt. No --service on an existing
    # secret = value-only rotation (metadata preserved).
    from durin.service.secrets import SecretsService
    from durin.service.types import ValidationFailedError

    try:
        if service is None:
            SecretsService().store_entry(name=name, value=value, rotate=True)
        else:
            SecretsService().store_entry(
                name=name,
                value=value,
                service=service,
                account=account or "",
                description=description,
                scope=_parse_scope(scope),
                origin="user",
            )
    except ValidationFailedError as exc:
        console.print(f"[red]✗[/red] {exc.message}")
        raise typer.Exit(1) from exc
    verb = "Updated" if existed else "Stored"
    detail = service if service is not None else "value rotated; metadata unchanged"
    console.print(f"[green]✓[/green] {verb} secret [bold]{name}[/bold] ({detail}).")


@secret_app.command("list")
def cmd_list() -> None:
    """List stored secrets — names and metadata only, never values."""
    store = SecretStore().load()
    entries = store.all()
    if not entries:
        console.print("[dim]No secrets stored. Add one with `durin secret set`.[/dim]")
        return
    table = Table(title="Stored secrets")
    table.add_column("Name", style="bold")
    table.add_column("Service")
    table.add_column("Account")
    table.add_column("Scope")
    table.add_column("Value")
    table.add_column("Origin", style="dim")
    for name, entry in sorted(entries.items()):
        table.add_row(
            name,
            entry.service,
            entry.account or "[dim]—[/dim]",
            ", ".join(entry.scope) or "[dim]—[/dim]",
            _mask(entry.value),
            entry.origin,
        )
    console.print(table)


@secret_app.command("show")
def cmd_show(
    name: str = typer.Argument(..., help="Secret name."),
    reveal: bool = typer.Option(
        False, "--reveal", help="Print the secret value in clear (use with care)."
    ),
) -> None:
    """Show one secret's metadata. `--reveal` prints the value."""
    store = SecretStore().load()
    entry = store.get(name)
    if entry is None:
        console.print(f"[red]✗[/red] No secret named '{name}'.")
        raise typer.Exit(1)
    console.print(f"[bold]{name}[/bold]")
    console.print(f"  service:     {entry.service}")
    console.print(f"  account:     {entry.account or '—'}")
    console.print(f"  description: {entry.description or '—'}")
    console.print(f"  scope:       {', '.join(entry.scope) or '—'}")
    console.print(f"  origin:      {entry.origin}")
    console.print(f"  created:     {entry.created_at}")
    if reveal:
        console.print(f"  [yellow]value:       {entry.value}[/yellow]")
    else:
        console.print(f"  value:       {_mask(entry.value)}  [dim](--reveal to show)[/dim]")


@secret_app.command("rm")
def cmd_rm(
    name: str = typer.Argument(..., help="Secret name to delete."),
) -> None:
    """Delete a secret from the store."""
    store = SecretStore().load()
    if not store.remove(name):
        console.print(f"[red]✗[/red] No secret named '{name}'.")
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] Removed secret [bold]{name}[/bold].")


@secret_app.command("grant")
def cmd_grant(
    name: str = typer.Argument(..., help="Secret name."),
    consumer: str = typer.Option(
        ..., "--to", help="Consumer tag to add (exec, skill:*, channel:telegram, …)."
    ),
) -> None:
    """Add a consumer tag to a secret's scope."""
    store = SecretStore()
    result = store.grant_consumer_locked(name, consumer)
    if result is None:
        console.print(f"[red]✗[/red] No secret named '{name}'.")
        raise typer.Exit(1)
    if result is False:
        console.print(f"[dim]{name} already grants '{consumer}'.[/dim]")
        return
    console.print(f"[green]✓[/green] {name} now grants [bold]{consumer}[/bold].")


@secret_app.command("migrate")
def cmd_migrate() -> None:
    """Move plaintext provider API keys from config into the store.

    One-shot and idempotent. The config keeps `${secret:…}` references
    afterwards; the values live in `secrets.json` (mode 0600).
    """
    from durin.security.secrets import migrate_plaintext_provider_keys

    created = migrate_plaintext_provider_keys()
    if not created:
        console.print(
            "[dim]Nothing to migrate — no plaintext provider keys in config.[/dim]"
        )
        return
    console.print(
        f"[green]✓[/green] Migrated {len(created)} key(s) into the secret store:"
    )
    for name in created:
        console.print(f"  • {name}")
    console.print(
        "[dim]Config now holds ${secret:…} references; the config backup is "
        "alongside it.[/dim]"
    )


@secret_app.command("revoke")
def cmd_revoke(
    name: str = typer.Argument(..., help="Secret name."),
    consumer: str = typer.Option(..., "--from", help="Consumer tag to remove."),
) -> None:
    """Remove a consumer tag from a secret's scope."""
    store = SecretStore()
    result = store.revoke_consumer_locked(name, consumer)
    if result is None:
        console.print(f"[red]✗[/red] No secret named '{name}'.")
        raise typer.Exit(1)
    if result is False:
        console.print(f"[dim]{name} does not grant '{consumer}'.[/dim]")
        return
    console.print(f"[green]✓[/green] {name} no longer grants [bold]{consumer}[/bold].")
