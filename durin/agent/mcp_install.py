"""MCP install orchestration.

Turns a registry ``McpServerDetail`` into a persisted ``MCPServerConfig`` (remote →
connect; local → spawn), detects the local runtime, and collects ``isSecret`` env
into durin's secret store as ``${secret:NAME}`` references. Shared by the webui
install path and the ``mcp_manage`` agent tool.
"""
from __future__ import annotations

import re
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


def _is_docker(pkg) -> bool:
    """True when the package launches via docker — an OCI image, or an explicit
    ``runtime_hint=="docker"``. OCI packages carry no runtime_hint in the registry."""
    return pkg.registry_type == "oci" or pkg.runtime_hint == "docker"


def package_runtime(pkg) -> str:
    """The effective runtime binary needed to launch this package (``docker`` for OCI)."""
    if _is_docker(pkg):
        return "docker"
    return pkg.runtime_hint or ""


def runtime_present(runtime_hint: str) -> bool:
    return shutil.which(_RUNTIME_BIN.get(runtime_hint, runtime_hint)) is not None


def runtime_install_spec(runtime_hint: str) -> dict | None:
    """Per-OS package names to install the runtime, or None when not auto-installable."""
    return _RUNTIME_INSTALL.get(runtime_hint)


def runtime_install_command(runtime_hint: str) -> str | None:
    """The shell command to install a runtime on this host, or None when it is not
    auto-installable (e.g. ``docker`` — a heavy runtime the user installs themselves)."""
    import sys

    spec = runtime_install_spec(runtime_hint)
    if not spec:
        return None
    if sys.platform == "darwin" and "brew" in spec:
        return f"brew install {spec['brew']}"
    if "apt" in spec:
        return f"apt-get install -y {spec['apt']}"
    if "brew" in spec:
        return f"brew install {spec['brew']}"
    return None


def _pinned_identifier(pkg) -> str:
    """Pin the package version into the launch arg so the server is reproducible.

    Without this, ``npx <pkg>`` / ``uvx <pkg>`` would resolve to @latest on every
    spawn — and the update-available check (`version` vs registry latest) would be
    meaningless.
    """
    if not pkg.version:
        return pkg.identifier
    if pkg.registry_type == "npm":
        return f"{pkg.identifier}@{pkg.version}"
    if pkg.registry_type == "pypi":
        return f"{pkg.identifier}=={pkg.version}"
    if pkg.registry_type == "oci":
        return f"{pkg.identifier}:{pkg.version}"
    return pkg.identifier


def _stdio_args(pkg) -> list[str]:
    ident = _pinned_identifier(pkg)
    if _is_docker(pkg):
        # Forward each env var into the container with a passthrough `-e NAME` flag —
        # the value lives in config.env (resolved at spawn), never in argv.
        e_flags = [tok for e in pkg.env for tok in ("-e", e.name)]
        return ["run", "-i", "--rm", *e_flags, *pkg.runtime_arguments, ident,
                *pkg.package_arguments]
    args = list(pkg.runtime_arguments)
    if pkg.runtime_hint == "npx" and "-y" not in args:
        args.insert(0, "-y")
    return [*args, ident, *pkg.package_arguments]


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
    rt = package_runtime(pkg)
    env = {e.name: (e.default or "") for e in pkg.env if not e.is_secret}
    env.update(secret_env_refs)
    return MCPServerConfig(
        type="stdio",
        command=_RUNTIME_BIN.get(rt, rt),
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


def _version_key(v: str) -> tuple:
    """Ordering key for a semver-ish version string. Splits off pre-release/build
    metadata so a pre-release sorts BELOW its release (1.0.0-rc.1 < 1.0.0) and the
    release core never inherits the rc/beta digits. Returns () for an unparseable
    version so ``has_update``'s guard treats it as "no nag"."""
    core = re.split(r"[-+]", v or "", maxsplit=1)[0]
    release = tuple(int(n) for n in re.findall(r"\d+", core))
    if not release:
        return ()
    remainder = (v or "")[len(core):]
    if remainder.startswith("-"):  # pre-release ranks below the final release
        return (release, 0, tuple(int(n) for n in re.findall(r"\d+", remainder)))
    return (release, 1)  # final release (build metadata, if any, is ignored)


def has_update(current: str, latest: str) -> bool:
    """True when ``latest`` is a strictly newer version than ``current``.

    Conservative: unparseable versions never flag an update (no false nags).
    """
    if not current or not latest:
        return False
    ck, lk = _version_key(current), _version_key(latest)
    if not ck or not lk:
        return False
    return lk > ck


def rebuild_for_update(old: MCPServerConfig, detail: McpServerDetail) -> MCPServerConfig:
    """Re-pin a configured local server to the registry's latest version, preserving
    the user's env/secrets/auth/customisations. Remote servers are returned unchanged
    (the provider owns their version)."""
    if old.type != "stdio" or not detail.packages:
        return old
    pkg = detail.packages[0]
    new = old.model_copy(deep=True)
    new.command = _RUNTIME_BIN.get(package_runtime(pkg), package_runtime(pkg))
    new.args = _stdio_args(pkg)
    new.version = pkg.version
    new.source_ref = detail.ref
    return new
