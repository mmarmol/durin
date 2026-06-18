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


@mcp_app.command("search")
def search(
    query: str = typer.Argument(..., help="What to search for (e.g. jira, postgres)."),
    limit: int = typer.Option(10, help="Max results."),
) -> None:
    """Search the MCP registry for installable servers."""
    from durin.agent.mcp_catalog_cache import McpCatalogCache
    from durin.agent.mcp_registry import build_mcp_adapters, search_mcp_registries
    from durin.config.loader import get_config_path

    disc = load_config().tools.mcp_discovery
    cache = McpCatalogCache(get_config_path().parent / "mcp_catalog.json")
    hits = asyncio.run(
        search_mcp_registries(
            query, cache=cache,
            adapters=build_mcp_adapters(disc.registries), limit=limit,
        )
    )
    if not hits:
        console.print("[dim]No servers found.[/dim]")
        return
    tags = {"remote": "no install", "both": "hosted/local", "local": "local"}
    for h in hits:
        console.print(f"  [bold]{h.ref}[/bold] [dim]({tags.get(h.kind, h.kind)})[/dim]")
        if h.description:
            console.print(f"    [dim]{h.description}[/dim]")
    console.print("\n[dim]Install with:[/dim] durin mcp install <ref>")


@mcp_app.command("install")
def install(
    ref: str = typer.Argument(..., help="Registry ref (from `durin mcp search`)."),
    prefer: str = typer.Option("remote", help="'remote' (hosted) or 'local' (installed)."),
) -> None:
    """Add an MCP server from the registry, prompting for any required config."""
    from durin.agent.mcp_registry import build_mcp_adapters
    from durin.service.mcp import McpRegistryInstallCommand, McpService
    from durin.service.principal import Principal

    disc = load_config().tools.mcp_discovery

    async def _describe():
        for adapter in build_mcp_adapters(disc.registries):
            detail = await adapter.describe(ref)
            if detail is not None:
                return detail
        return None

    detail = asyncio.run(_describe())
    if detail is None:
        console.print(f"[red]✗[/red] not found in registry: {ref}")
        raise typer.Exit(1)

    use_local = (prefer == "local" and detail.packages) or (
        not detail.remotes and detail.packages
    )
    src_env = (
        detail.packages[0].env if (use_local and detail.packages)
        else (detail.remotes[0].headers if detail.remotes else [])
    )
    env_values: dict[str, str] = {}
    for e in src_env:
        if e.is_required or e.is_secret:
            label = e.name + (f" ({e.description})" if e.description else "")
            env_values[e.name] = typer.prompt(label, hide_input=e.is_secret, default="")

    try:
        result = asyncio.run(
            McpService().registry_install(
                McpRegistryInstallCommand(
                    ref=ref, prefer=prefer, env_values=env_values or None
                ),
                Principal.local(),
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from None
    console.print(
        f"[green]✓[/green] Added [bold]{result.name}[/bold] "
        f"({result.transport}) — status: {result.status}"
    )
    if result.status == "needs_auth":
        console.print(f"[dim]Sign in with:[/dim] durin mcp login {result.name}")
