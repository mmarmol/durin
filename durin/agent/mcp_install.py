"""MCP install orchestration.

Turns a registry ``McpServerDetail`` into a persisted ``MCPServerConfig`` (remote →
connect; local → spawn), detects the local runtime, and collects ``isSecret`` env
into durin's secret store as ``${secret:NAME}`` references. Shared by the webui
install path and the ``mcp_manage`` agent tool.
"""
from __future__ import annotations

import shutil

from durin.agent.mcp_registry import McpServerDetail
from durin.config.schema import MCPServerConfig

_TRANSPORT = {"streamable-http": "streamableHttp", "sse": "sse", "stdio": "stdio"}
_RUNTIME_BIN = {"npx": "npx", "uvx": "uvx", "docker": "docker"}
# runtime_hint -> per-OS package names to install the runtime (skills install-specs
# managers). docker is intentionally absent — heavy runtime; prefer the remote variant.
_RUNTIME_INSTALL = {
    "npx": {"brew": "node", "apt": "nodejs"},
    "uvx": {"brew": "uv", "apt": "uv"},
}


def runtime_present(runtime_hint: str) -> bool:
    return shutil.which(_RUNTIME_BIN.get(runtime_hint, runtime_hint)) is not None


def runtime_install_spec(runtime_hint: str) -> dict | None:
    """Per-OS package names to install the runtime, or None when not auto-installable."""
    return _RUNTIME_INSTALL.get(runtime_hint)


def _stdio_args(pkg) -> list[str]:
    if pkg.runtime_hint == "docker":
        return ["run", "-i", "--rm", *pkg.runtime_arguments, pkg.identifier,
                *pkg.package_arguments]
    args = list(pkg.runtime_arguments)
    if pkg.runtime_hint == "npx" and "-y" not in args:
        args.insert(0, "-y")
    return [*args, pkg.identifier, *pkg.package_arguments]


def build_server_config_from_detail(
    detail: McpServerDetail, *, prefer: str, secret_env_refs: dict[str, str]
) -> MCPServerConfig:
    """Build a persisted server config; fall back to whichever model the server offers.

    Secret env values are supplied as ``${secret:NAME}`` references in ``secret_env_refs``.
    """
    use_remote = (prefer == "remote" and detail.remotes) or (
        not detail.packages and detail.remotes
    )
    if use_remote:
        remote = detail.remotes[0]
        return MCPServerConfig(
            type=_TRANSPORT.get(remote.transport_type, "streamableHttp"),
            url=remote.url,
            source_ref=detail.ref,
        )
    if not detail.packages:
        raise ValueError(f"server '{detail.ref}' has neither packages nor remotes")
    pkg = detail.packages[0]
    env = {e.name: (e.default or "") for e in pkg.env if not e.is_secret}
    env.update(secret_env_refs)
    return MCPServerConfig(
        type="stdio",
        command=_RUNTIME_BIN.get(pkg.runtime_hint, pkg.runtime_hint),
        args=_stdio_args(pkg),
        env=env,
        version=pkg.version,
        source_ref=detail.ref,
    )


def collect_secret_env(
    detail: McpServerDetail, values: dict[str, str], *, server_name: str
) -> dict[str, str]:
    """Store ``isSecret`` env/header values; return ``{name: ${secret:NAME}}``.

    Non-secret values are NOT stored here (they go inline via
    ``build_server_config_from_detail``). The agent never supplies these values — the
    human does (form input / paste).
    """
    from durin.security.secrets import store_secret

    secret_names = {e.name for p in detail.packages for e in p.env if e.is_secret}
    secret_names |= {e.name for r in detail.remotes for e in r.headers if e.is_secret}
    refs: dict[str, str] = {}
    for name, val in (values or {}).items():
        if name in secret_names and val:
            refs[name] = store_secret(
                f"MCP_{server_name.upper()}_{name}",
                val,
                service=f"mcp:{server_name}",
                scope=[f"mcp:{server_name}"],
                origin="user",
            )
    return refs
