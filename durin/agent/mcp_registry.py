"""MCP registry adapters + data model.

Mirrors ``durin/agent/skill_registry.py``: a small dataclass model plus an
``McpRegistry`` Protocol that concrete adapters (``OfficialMcpRegistry``, and
later ``MpakRegistry``) implement. Search is cache-backed (the official
registry's own search is substring-on-name only — see
``durin/agent/mcp_catalog_cache.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


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


def parse_server_json(obj: dict) -> McpServerDetail:
    """Parse one registry ``server.json`` object into an ``McpServerDetail``."""
    repo = obj.get("repository") or {}
    packages: list[PackageSpec] = []
    for p in obj.get("packages") or []:
        tr = p.get("transport") or {}
        packages.append(
            PackageSpec(
                registry_type=p.get("registryType", ""),
                identifier=p.get("identifier", ""),
                version=p.get("version", ""),
                runtime_hint=p.get("runtimeHint", ""),
                transport_type=tr.get("type", "stdio"),
                runtime_arguments=_arg_values(p.get("runtimeArguments")),
                package_arguments=_arg_values(p.get("packageArguments")),
                env=_env_specs(p.get("environmentVariables")),
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
    return McpServerHit(
        name=obj.get("name", ""),
        ref=obj.get("name", ""),
        registry=registry,
        kind=kind,
        description=obj.get("description", ""),
    )
