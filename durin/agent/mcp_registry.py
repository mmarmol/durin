"""MCP registry adapters + data model.

Mirrors ``durin/agent/skill_registry.py``: a small dataclass model plus an
``McpRegistry`` Protocol that concrete adapters (``OfficialMcpRegistry``, and
later ``MpakRegistry``) implement.

Search reads the durin-owned catalog store (``durin/agent/mcp_catalog_store.py``),
which ranks fuzzily over the vendored floor + downloaded overlay. The registry
adapters here are used by INSTALL/describe (``mcp_manage``) and by the catalog
build script — not by search.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Protocol

_TEMPLATE_RE = re.compile(r"\{([^}]+)\}")


@dataclass
class EnvVarSpec:
    """One environment variable / config input declared by a server.

    ``is_secret`` drives whether the install form masks the field and stores the
    value in durin's secret store as a ``${secret:NAME}`` reference.
    """

    name: str
    description: str = ""
    is_required: bool = False
    is_secret: bool = False
    default: str | None = None


@dataclass
class PackageSpec:
    """A locally-installable package (stdio): how to launch the server."""

    registry_type: str          # npm | pypi | oci | ...
    identifier: str
    version: str
    runtime_hint: str           # npx | uvx | docker | ...
    transport_type: str         # stdio | streamable-http | sse
    runtime_arguments: list[str] = field(default_factory=list)
    package_arguments: list[str] = field(default_factory=list)
    env: list[EnvVarSpec] = field(default_factory=list)


@dataclass
class RemoteSpec:
    """A hosted server endpoint: connect over HTTP, no local process."""

    transport_type: str         # streamable-http | sse
    url: str
    headers: list[EnvVarSpec] = field(default_factory=list)


@dataclass
class McpServerHit:
    """A search result row. ``kind`` is derived from packages/remotes presence."""

    name: str
    ref: str
    registry: str
    kind: str                   # "remote" | "local" | "both"
    description: str = ""
    signals: dict = field(default_factory=dict)  # e.g. {"trust_level": "L3"} (mpak, fast-follow)


@dataclass
class McpServerDetail:
    """Full install metadata for one server (parsed server.json slice)."""

    name: str
    ref: str
    description: str
    version: str
    repository: str
    packages: list[PackageSpec] = field(default_factory=list)
    remotes: list[RemoteSpec] = field(default_factory=list)


class McpRegistry(Protocol):
    """Adapter contract — mirrors ``SkillRegistry``."""

    name: str

    async def search(self, query: str, *, limit: int) -> list[McpServerHit]: ...

    async def describe(self, ref: str) -> McpServerDetail | None: ...


def _env_specs(items: list[dict] | None) -> list[EnvVarSpec]:
    out: list[EnvVarSpec] = []
    for it in items or []:
        out.append(
            EnvVarSpec(
                name=it.get("name", ""),
                description=it.get("description", ""),
                is_required=bool(it.get("isRequired")),
                is_secret=bool(it.get("isSecret")),
                default=it.get("default"),
            )
        )
    return out


def _arg_values(items: list[dict] | None) -> list[str]:
    return [str(it.get("value", it.get("name", ""))) for it in (items or []) if it]


def _render_arg(it: dict) -> list[str]:
    """Render one registry argument object to argv tokens, substituting ``{var}``
    templates with their declared default. Named → ``[name, value]``; positional →
    ``[value]``."""
    name = str(it.get("name", ""))
    value = str(it.get("value", ""))
    variables = it.get("variables") or {}
    rendered = _TEMPLATE_RE.sub(
        lambda m: str((variables.get(m.group(1)) or {}).get("default", m.group(0))),
        value,
    )
    out: list[str] = []
    if name and name != value:
        out.append(name)
    if rendered:
        out.append(rendered)
    return out


def _parse_oci_runtime_args(
    items: list[dict] | None,
) -> tuple[list[EnvVarSpec], list[str]]:
    """Split an OCI package's ``runtimeArguments`` into (env inputs, passthrough argv).

    Docker servers declare env injection as a ``-e NAME={var}`` named arg whose
    ``{var}`` is typed in ``variables`` (e.g. github's secret token). These are lifted
    into :class:`EnvVarSpec` so the install form prompts for them and the secret store
    collects them; at launch they become a passthrough ``-e NAME`` flag (the value
    lives in env, resolved at spawn). Every other arg passes through verbatim.
    """
    env_specs: list[EnvVarSpec] = []
    passthrough: list[str] = []
    for it in items or []:
        if not it:
            continue
        if it.get("name") == "-e" and it.get("value"):
            env_name, _, rhs = str(it["value"]).partition("=")
            env_name = env_name.strip()
            if not env_name:
                continue
            variables = it.get("variables") or {}
            tmpl = _TEMPLATE_RE.search(rhs)
            var = variables.get(tmpl.group(1), {}) if tmpl else {}
            env_specs.append(
                EnvVarSpec(
                    name=env_name,
                    description=it.get("description", ""),
                    is_required=bool(var.get("isRequired", it.get("isRequired"))),
                    is_secret=bool(var.get("isSecret")),
                    default=var.get("default") if tmpl else (rhs or None),
                )
            )
        else:
            passthrough.extend(_render_arg(it))
    return env_specs, passthrough


def parse_server_json(obj: dict) -> McpServerDetail:
    """Parse one registry ``server.json`` object into an ``McpServerDetail``."""
    repo = obj.get("repository") or {}
    packages: list[PackageSpec] = []
    for p in obj.get("packages") or []:
        tr = p.get("transport") or {}
        env = _env_specs(p.get("environmentVariables"))
        if p.get("registryType") == "oci":
            # OCI/docker declares env (and secrets) inside `-e NAME={var}` runtime args
            # rather than environmentVariables — lift those into env inputs.
            oci_env, runtime_arguments = _parse_oci_runtime_args(p.get("runtimeArguments"))
            env = env + oci_env
        else:
            runtime_arguments = _arg_values(p.get("runtimeArguments"))
        packages.append(
            PackageSpec(
                registry_type=p.get("registryType", ""),
                identifier=p.get("identifier", ""),
                version=p.get("version", ""),
                runtime_hint=p.get("runtimeHint", ""),
                transport_type=tr.get("type", "stdio"),
                runtime_arguments=runtime_arguments,
                package_arguments=_arg_values(p.get("packageArguments")),
                env=env,
            )
        )
    remotes: list[RemoteSpec] = []
    for r in obj.get("remotes") or []:
        remotes.append(
            RemoteSpec(
                transport_type=r.get("type", ""),
                url=r.get("url", ""),
                headers=_env_specs(r.get("headers")),
            )
        )
    return McpServerDetail(
        name=obj.get("name", ""),
        ref=obj.get("name", ""),
        description=obj.get("description", ""),
        version=obj.get("version", ""),
        repository=repo.get("url", ""),
        packages=packages,
        remotes=remotes,
    )


def _hit_from_server(obj: dict, *, registry: str) -> McpServerHit:
    """Build a lightweight search hit; ``kind`` from packages/remotes presence."""
    has_pkg = bool(obj.get("packages"))
    has_remote = bool(obj.get("remotes"))
    kind = "both" if (has_pkg and has_remote) else ("local" if has_pkg else "remote")
    gh = obj.get("_github") or {}
    signals = {k: gh[k] for k in (
        "stars", "owner_login", "owner_type", "owner_url", "owner_avatar",
        "topics", "language", "license",
    ) if k in gh}
    if "official" in obj:
        signals["official"] = obj["official"]
    if (obj.get("repository") or {}).get("url"):
        signals["repo_url"] = obj["repository"]["url"]
    return McpServerHit(
        name=obj.get("name", ""),
        ref=obj.get("name", ""),
        registry=registry,
        kind=kind,
        description=obj.get("description", ""),
        signals=signals,
    )


class _DefaultHTTP:
    """Minimal async JSON GET, used when no http client is injected."""

    async def get_json(self, url: str) -> dict:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()


class OfficialMcpRegistry:
    """Adapter for the official MCP registry (registry.modelcontextprotocol.io).

    No auth. Its ``search`` param is substring-on-name only; breadth/quality
    search lives in the catalog store. ``fetch_page`` here is the cursor-paginated
    crawl the catalog build script uses to (re)generate that store's overlay.
    """

    name = "official"
    BASE = "https://registry.modelcontextprotocol.io"

    def __init__(self, http=None) -> None:
        self._http = http or _DefaultHTTP()

    async def search(self, query: str, *, limit: int) -> list[McpServerHit]:
        q = urllib.parse.urlencode({"search": query, "limit": min(limit, 100), "version": "latest"})
        data = await self._http.get_json(f"{self.BASE}/v0/servers?{q}")
        hits = [
            _hit_from_server(e.get("server") or {}, registry=self.name)
            for e in (data.get("servers") or [])
        ]
        return [h for h in hits if h.ref][:limit]

    async def describe(self, ref: str) -> McpServerDetail | None:
        url = f"{self.BASE}/v0/servers/{urllib.parse.quote(ref, safe='')}/versions/latest"
        try:
            data = await self._http.get_json(url)
        except Exception:  # noqa: BLE001
            return None
        return parse_server_json(data.get("server") or data)

    async def fetch_page(self, *, cursor: str | None = None, updated_since: str | None = None):
        params: dict = {"limit": 100, "version": "latest"}
        if cursor:
            params["cursor"] = cursor
        if updated_since:
            params["updated_since"] = updated_since
        data = await self._http.get_json(f"{self.BASE}/v0/servers?{urllib.parse.urlencode(params)}")
        servers = [e.get("server") or {} for e in (data.get("servers") or [])]
        return servers, (data.get("metadata") or {}).get("nextCursor")


def _normalize_github_server(obj: dict) -> dict:
    """Map a GitHub MCP Registry server object to the official ``server.json`` shape.

    The GitHub registry (``api.mcp.github.com``) uses snake_case package fields and a
    lighter schema; normalising lets ``parse_server_json`` / ``_hit_from_server`` consume
    it unchanged.
    """
    s = obj.get("server", obj)
    pkgs = []
    for p in s.get("packages") or []:
        pkgs.append({
            "registryType": p.get("registry_name") or p.get("registryType", ""),
            "identifier": p.get("name") or p.get("identifier", ""),
            "version": p.get("version", ""),
            "runtimeHint": p.get("runtime_hint") or p.get("runtimeHint", ""),
            "transport": p.get("transport") or {"type": "stdio"},
            "runtimeArguments": p.get("runtime_arguments") or p.get("runtimeArguments") or [],
            "packageArguments": p.get("package_arguments") or p.get("packageArguments") or [],
            "environmentVariables": (
                p.get("environment_variables") or p.get("environmentVariables") or []
            ),
        })
    remotes = []
    for r in s.get("remotes") or []:
        remotes.append({
            "type": r.get("type") or r.get("transport_type", ""),
            "url": r.get("url", ""),
            "headers": r.get("headers") or [],
        })
    vd = s.get("version_detail") or {}
    return {
        "name": s.get("name", ""),
        "description": s.get("description", ""),
        "repository": s.get("repository") or {},
        "version": s.get("version") or vd.get("version", ""),
        "packages": pkgs,
        "remotes": remotes,
    }


class GithubMcpRegistry:
    """Adapter for GitHub's curated MCP registry (``api.mcp.github.com``).

    GitHub vets this set (its servers carry a ``verified`` signal in the catalog). The
    list is small (~hundreds) and its ``search`` param is non-functional, so ``describe``
    fetches the full list once, indexes it by name, and serves install metadata from
    there (normalised to the official ``server.json`` shape).
    """

    name = "github"
    BASE = "https://api.mcp.github.com"

    def __init__(self, http=None) -> None:
        self._http = http or _DefaultHTTP()
        self._by_name: dict[str, dict] | None = None

    async def _index(self) -> dict[str, dict]:
        if self._by_name is None:
            self._by_name = {}
            async for server in self._iter_servers():
                norm = _normalize_github_server(server)
                if norm["name"]:
                    self._by_name[norm["name"]] = norm
        return self._by_name

    async def _iter_servers(self):
        cursor = None
        for _ in range(100):  # hard page cap — backstop, not a real bound
            params: dict = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            url = f"{self.BASE}/v0/servers?{urllib.parse.urlencode(params)}"
            try:
                data = await self._http.get_json(url)
            except Exception:  # noqa: BLE001
                return
            for e in data.get("servers") or []:
                yield e.get("server") or e
            cursor = (data.get("metadata") or {}).get("next_cursor")
            if not cursor:
                return

    async def search(self, query: str, *, limit: int) -> list[McpServerHit]:
        index = await self._index()
        return [_hit_from_server(s, registry=self.name) for s in list(index.values())[:limit]]

    async def describe(self, ref: str) -> McpServerDetail | None:
        index = await self._index()
        obj = index.get(ref)
        return parse_server_json(obj) if obj else None

    async def fetch_page(self, *, cursor: str | None = None, updated_since: str | None = None):
        params: dict = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = await self._http.get_json(f"{self.BASE}/v0/servers?{urllib.parse.urlencode(params)}")
        servers = [_normalize_github_server(e.get("server") or e) for e in (data.get("servers") or [])]
        return servers, (data.get("metadata") or {}).get("next_cursor")


def build_mcp_adapters(registries) -> list:
    """Instantiate enabled registry adapters (mirror of ``skill_registry.build_adapters``).

    ``official`` is the broad install-grade source; ``github`` is GitHub's curated set
    (used as the ``verified`` tier + an install fallback for servers only GitHub lists).
    ``official`` is built first so its richer metadata wins on ``describe``.
    """
    out: list = []
    for r in registries:
        if not getattr(r, "enabled", True):
            continue
        if r.kind == "official":
            out.append(OfficialMcpRegistry())
        # elif r.kind == "mpak": out.append(MpakRegistry())  # fast-follow
    out.append(GithubMcpRegistry())  # curated fallback — always on (no token, public API)
    return out


async def search_mcp_registries(query, *, limit, quality="official", min_stars=100):
    """Search the durin-owned catalog store (floor + overlay), ranked fuzzily.

    Kept ``async`` so the ``await`` call-sites stay unchanged; the store's
    ``search`` is synchronous (a small in-memory rank over a vendored catalog).
    """
    from durin.agent import mcp_catalog_store

    return mcp_catalog_store.search(query, limit=limit, quality=quality, min_stars=min_stars)
