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


# Registry package type -> the runtime that launches it, used when the server omits
# runtimeHint (common: microsoft/playwright-mcp is npm with an empty hint; github is
# oci with an empty hint). An explicit runtime_hint always wins over this.
_REGISTRY_RUNTIME = {"npm": "npx", "pypi": "uvx", "oci": "docker"}


def package_runtime(pkg) -> str:
    """The effective runtime binary to launch this package. An explicit ``runtime_hint``
    wins; otherwise infer from the registry type (npm→npx, pypi→uvx, oci→docker), since
    the registry frequently omits the hint (which would otherwise yield an empty command
    → a 422 on install)."""
    return pkg.runtime_hint or _REGISTRY_RUNTIME.get(pkg.registry_type, "")


def _is_docker(pkg) -> bool:
    """True when the package launches via docker (an OCI image or ``runtime_hint=="docker"``)."""
    return package_runtime(pkg) == "docker"


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
    if package_runtime(pkg) == "npx" and "-y" not in args:
        args.insert(0, "-y")
    return [*args, ident, *pkg.package_arguments]


def build_server_config_from_detail(
    detail: McpServerDetail, *, prefer: str, secret_env_refs: dict[str, str]
) -> MCPServerConfig:
    """Build a persisted server config; fall back to whichever model the server offers.

    Secret values (collected by ``collect_secret_env``) are supplied as ``${secret:NAME}``
    references in ``secret_env_refs`` and applied to the local server's ``env`` or the remote
    server's ``headers`` (matched by name) — resolved to plaintext at spawn/connect time.
    """
    use_remote = (prefer == "remote" and detail.remotes) or (
        not detail.packages and detail.remotes
    )
    if use_remote:
        remote = detail.remotes[0]
        # Apply the remote's declared headers: non-secret defaults inline + the collected
        # secret refs (e.g. a static ``Authorization`` token). Without this the user-supplied
        # token is dropped and the remote 401s.
        header_names = {h.name for h in remote.headers}
        headers = {h.name: (h.default or "") for h in remote.headers if not h.is_secret}
        headers.update({k: v for k, v in secret_env_refs.items() if k in header_names})
        return MCPServerConfig(
            type=_TRANSPORT.get(remote.transport_type, "streamableHttp"),
            url=remote.url,
            headers=headers,
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


def _default_probe():
    """The production 401-probe: POST an MCP `initialize` and return (status, www-authenticate)."""
    async def request(u: str):  # noqa: ANN001
        import httpx

        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "durin", "version": "0"}},
        }
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as c:
            r = await c.post(u, json=payload, headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            })
        return r.status_code, r.headers.get("www-authenticate", "")

    return request


def _default_fetch_json():
    async def fetch_json(u: str):  # noqa: ANN001
        import httpx

        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as c:
            r = await c.get(u, headers={"Accept": "application/json"})
        return r.json() if r.status_code == 200 else {}

    return fetch_json


def _parse_resource_metadata(www: str) -> str:
    """Pull the RFC 9728 ``resource_metadata="<url>"`` value out of a WWW-Authenticate header."""
    m = re.search(r'resource_metadata="([^"]+)"', www or "")
    return m.group(1) if m else ""


def _as_metadata_urls(as_url: str) -> list[str]:
    """The two well-known authorization-server metadata locations to try (RFC 8414 + OIDC)."""
    base = (as_url or "").rstrip("/")
    return [base + "/.well-known/oauth-authorization-server",
            base + "/.well-known/openid-configuration"]


async def _discover_dcr(www: str, fetch_json) -> bool:
    """Follow resource_metadata → authorization-server metadata; True if a ``registration_endpoint``
    (Dynamic Client Registration) is advertised. Best-effort: any miss/error → False."""
    prm_url = _parse_resource_metadata(www)
    if not prm_url:
        return False
    try:
        prm = await fetch_json(prm_url)
    except Exception:  # noqa: BLE001
        return False
    for as_url in (prm.get("authorization_servers") if isinstance(prm, dict) else None) or []:
        for meta_url in _as_metadata_urls(as_url):
            try:
                meta = await fetch_json(meta_url)
            except Exception:  # noqa: BLE001
                meta = {}
            if isinstance(meta, dict) and meta.get("registration_endpoint"):
                return True
    return False


async def remote_needs_oauth(url: str, *, request=None) -> bool:
    """True when the remote MCP endpoint answers an unauthenticated request with a 401
    ``WWW-Authenticate: Bearer`` challenge (MCP/RFC-9728). Bounded + best-effort: any error
    or non-401 → False. ``request`` is an injectable ``async (url) -> (status, www)`` seam."""
    if not url:
        return False
    if request is None:
        request = _default_probe()
    try:
        status, www = await request(url)
    except Exception:  # noqa: BLE001 — unreachable/slow → don't force oauth
        return False
    return status == 401 and "bearer" in (www or "").lower()


async def remote_oauth_capability(url: str, *, request=None, fetch_json=None) -> dict:
    """Probe whether durin can complete **zero-secret** OAuth against ``url``.

    Returns ``{"oauth": bool, "dcr": bool}``. ``oauth`` is the 401-Bearer signal;
    ``dcr`` follows the RFC 9728 chain and is True when the authorization server
    advertises a ``registration_endpoint`` (Dynamic Client Registration) — the only
    way durin can OAuth without shipping any credential. Best-effort: any error → both
    False. ``request``/``fetch_json`` are injectable seams for tests."""
    if not url:
        return {"oauth": False, "dcr": False}
    if request is None:
        request = _default_probe()
    try:
        status, www = await request(url)
    except Exception:  # noqa: BLE001
        return {"oauth": False, "dcr": False}
    if not (status == 401 and "bearer" in (www or "").lower()):
        return {"oauth": False, "dcr": False}
    if fetch_json is None:
        fetch_json = _default_fetch_json()
    return {"oauth": True, "dcr": await _discover_dcr(www, fetch_json)}


def _auth_help_map() -> dict:
    import json
    from pathlib import Path

    path = Path(__file__).parent / "data" / "mcp_auth_help.json"
    try:
        return json.loads(path.read_text("utf-8")).get("entries", {})
    except Exception:  # noqa: BLE001
        return {}


def apply_auth_help(detail) -> None:
    """Attach a curated ``help_url`` to each secret input the catalog knows about.

    Keyed by ``(registry ref, input name)``. Pure no-op on a miss — the field stays
    ``None`` and the form falls back to the input's (linkified) description."""
    by_input = _auth_help_map().get(detail.ref) or {}
    if not by_input:
        return
    inputs = [e for p in detail.packages for e in p.env]
    inputs += [e for r in detail.remotes for e in r.headers]
    for e in inputs:
        url = by_input.get(e.name)
        if url:
            e.help_url = url


async def autodetect_oauth(
    sc: MCPServerConfig, *, has_declared_headers: bool = False, request=None, fetch_json=None
) -> None:
    """Enable OAuth on a freshly-built REMOTE config only when durin can complete it.

    Skips stdio servers, configs that already declare a static auth header (e.g. github's
    PAT) or already set ``oauth``. Sets ``oauth=True`` only when the endpoint advertises
    zero-secret OAuth via DCR — so a 401-Bearer endpoint without DCR (e.g. GitHub) is left
    on the token path instead of flipping to a flow that would fail at sign-in. Mutates
    ``sc`` in place. (A server pre-configured with an ``oauth.client_id`` is already
    ``oauth``-set and returns at the guard above.)"""
    if sc.command or not sc.url or sc.oauth or has_declared_headers:
        return
    cap = await remote_oauth_capability(sc.url, request=request, fetch_json=fetch_json)
    if cap["dcr"]:
        sc.oauth = True
