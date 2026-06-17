"""`durin mcp` — manage remote MCP server connections (OAuth sign-in).

`durin mcp login <server>` runs the interactive OAuth flow for a server marked
`oauth` in config, persisting tokens to the durin secret store. This is the one
place a browser is opened — agent runs never do (they surface this command
instead).
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console

from durin.config.loader import load_config

console = Console()

mcp_app = typer.Typer(
    help="Manage remote MCP servers (OAuth sign-in).",
    no_args_is_help=True,
)


def _resolve_server(name: str):
    """Return (cfg, MCPOAuthConfig) for an oauth-marked server, or exit."""
    config = load_config()
    servers = config.tools.mcp_servers
    cfg = servers.get(name)
    if cfg is None:
        console.print(
            f"[red]✗[/red] MCP server '{name}' is not configured. "
            f"Known: {', '.join(servers) or '(none)'}"
        )
        raise typer.Exit(1)
    oc = cfg.oauth_config()
    if oc is None:
        console.print(
            f"[red]✗[/red] MCP server '{name}' is not marked oauth. "
            f"Add `\"oauth\": true` to its config first."
        )
        raise typer.Exit(1)
    return cfg, oc


async def _run_login_flow(server: str, cfg) -> None:
    """Drive the SDK OAuth flow once, end to end, persisting tokens.

    Builds the provider with interactive handlers + a loopback callback, then
    forces a single authenticated request so async_auth_flow runs the full
    handshake. Uses the SDK's own ClientSession against the configured URL.
    """
    from durin.agent.tools.mcp_oauth import (
        LoopbackCallback,
        build_oauth_provider,
        drive_oauth_handshake,
        make_interactive_handlers,
    )

    oc = cfg.oauth_config()
    callback = LoopbackCallback(port=oc.callback_port)
    callback.start()
    redirect_h, callback_h = make_interactive_handlers(callback)
    provider = build_oauth_provider(
        server,
        cfg,
        headless=False,
        redirect_handler=redirect_h,
        callback_handler=callback_h,
    )
    try:
        # Transport-aware: SSE servers must not be driven over streamable-HTTP.
        await drive_oauth_handshake(provider, cfg)
    finally:
        callback.stop()


@mcp_app.command("login")
def login(
    server: str = typer.Argument(..., help="Configured MCP server name."),
) -> None:
    """Run the interactive OAuth sign-in for a remote MCP server."""
    cfg, _oc = _resolve_server(server)
    console.print(f"OAuth sign-in for MCP server [bold]{server}[/bold] ({cfg.url})")
    try:
        asyncio.run(_run_login_flow(server, cfg))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗[/red] Sign-in failed: {exc}")
        raise typer.Exit(1) from None
    console.print(f"[green]✓[/green] Signed in to [bold]{server}[/bold].")


@mcp_app.command("logout")
def logout(
    server: str = typer.Argument(..., help="Configured MCP server name."),
) -> None:
    """Forget stored OAuth tokens for a remote MCP server."""
    cfg, _oc = _resolve_server(server)
    from durin.agent.tools.mcp_oauth import SecretsTokenStorage

    st = SecretsTokenStorage(server, server_url=cfg.url or None)
    if st.forget():
        console.print(f"[green]✓[/green] Forgot OAuth tokens for [bold]{server}[/bold].")
    else:
        console.print(f"[dim]No stored OAuth tokens for {server}.[/dim]")


@mcp_app.command("status")
def status() -> None:
    """Show OAuth token presence for each oauth-marked MCP server."""
    from durin.agent.tools.mcp_oauth import SecretsTokenStorage

    config = load_config()
    rows = []
    for name, cfg in config.tools.mcp_servers.items():
        if cfg.oauth_config() is None:
            continue
        st = SecretsTokenStorage(name, server_url=cfg.url or None)
        present = asyncio.run(st.get_tokens()) is not None
        rows.append((name, present))
    if not rows:
        console.print("[dim]No oauth-marked MCP servers configured.[/dim]")
        return
    for name, present in rows:
        mark = "[green]✓ signed in[/green]" if present else "[yellow]— not signed in[/yellow]"
        console.print(f"  {name}: {mark}")
