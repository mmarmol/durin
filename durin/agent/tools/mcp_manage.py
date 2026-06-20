"""mcp_manage tool — gated create/modify/install of MCP servers.

The single WRITE counterpart to ``mcp_search``. Wraps ``McpService`` (add / update /
remove / enable / disable / reconnect) and the registry install path, so the agent can
act on a user's conversational request ("add an MCP at this URL", "raise that timeout",
"remove it", "install the jira server we found").

Gating: the introduce/modify actions (install / add / update) honour
``tools.mcp_discovery.install_policy`` — ``never`` refuses, ``approve`` (default) returns a
dry-run preview unless ``confirm=true``, ``auto`` proceeds. Adding a server wires up a new
tool source, so this human confirm is a real security control (an injected prompt could try
to add a malicious server). Secrets are always supplied by the human (OAuth login / pasted
values); the agent never provides a credential. Runtime install (e.g. ``brew install node``
for a local server whose runtime is missing) runs through the ExecTool gate.
"""
from __future__ import annotations

from typing import Any

from durin.agent.mcp_registry import build_mcp_adapters
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

_GATED = {"install", "add", "update"}

_PARAMETERS = tool_parameters_schema(
    action=StringSchema(
        "One of: install (from a registry ref), add (explicit config), update, remove, "
        "enable, disable, reconnect."
    ),
    ref=StringSchema("Registry ref for action=install (from mcp_search)."),
    name=StringSchema("Server name for add/update/remove/enable/disable/reconnect."),
    config=StringSchema("JSON object of MCPServerConfig fields for action=add/update."),
    prefer=StringSchema("For install: 'remote' (default) or 'local'."),
    confirm=StringSchema("Set 'true' to execute a gated action under install_policy=approve."),
    description=(
        "Create, modify, install, or remove an MCP server. Discover refs first with "
        "mcp_search. install/add/update are gated by install_policy (approve = dry-run "
        "then confirm). Remote installs hand off to a human OAuth login; secrets are "
        "entered by the human, never the agent."
    ),
)


def _name_from_ref(ref: str) -> str:
    return (ref.rsplit("/", 1)[-1] or ref).strip()


@tool_parameters(_PARAMETERS)
class McpManageTool(Tool):
    """mcp_manage tool — gated MCP server CRUD + registry install."""

    def __init__(self, *, service, exec_run=None, install_policy="approve",
                 registries=None) -> None:
        self._service = service
        self._exec_run = exec_run
        self._policy = install_policy
        self._registries = list(registries or [])

    @property
    def name(self) -> str:
        return "mcp_manage"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @property
    def read_only(self) -> bool:
        return False

    @classmethod
    def create(cls, ctx: Any) -> "McpManageTool":
        from durin.agent.tools.shell import ExecTool
        from durin.service.mcp import McpService

        try:
            disc = ctx.app_config.tools.mcp_discovery
        except Exception:  # noqa: BLE001
            from durin.config.loader import load_config

            disc = load_config().tools.mcp_discovery
        runtime = getattr(ctx, "mcp_runtime", None)
        return cls(
            service=McpService(mcp_runtime=runtime),
            exec_run=ExecTool.create(ctx).execute,
            install_policy=disc.install_policy,
            registries=list(disc.registries),
        )

    def _gate(self, action: str, kwargs: dict) -> str:
        """Return 'run' | 'dry' | 'refuse' for a gated action."""
        if action not in _GATED:
            return "run"
        if self._policy == "never":
            return "refuse"
        if self._policy == "auto" or str(kwargs.get("confirm", "")).lower() == "true":
            return "run"
        return "dry"

    async def execute(self, **kwargs: Any) -> Any:
        from durin.service.principal import Principal

        action = str(kwargs.get("action", "")).strip()
        principal = Principal.local()
        try:
            if action == "install":
                return await self._install(kwargs, principal)
            if action == "add":
                return await self._add(kwargs, principal)
            if action == "update":
                return await self._add(kwargs, principal, update=True)
            if action in {"remove", "enable", "disable", "reconnect"}:
                return await self._name_action(action, kwargs, principal)
            return {"error": f"unknown action: {action!r}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    async def _name_action(self, action: str, kwargs: dict, principal) -> Any:
        from durin.service.mcp import McpServerNameCommand

        name = str(kwargs.get("name", "")).strip()
        if not name:
            return {"error": "name is required"}
        method = getattr(self._service, action)
        result = await method(McpServerNameCommand(name=name), principal)
        return {"name": name, "result": _as_dict(result)}

    async def _add(self, kwargs: dict, principal, *, update: bool = False) -> Any:
        import json

        from durin.config.schema import MCPServerConfig
        from durin.service.mcp import McpServerUpsertCommand

        name = str(kwargs.get("name", "")).strip()
        raw = kwargs.get("config") or {}
        if isinstance(raw, str):
            raw = json.loads(raw) if raw.strip() else {}
        if not name:
            return {"error": "name is required"}
        gate = self._gate("update" if update else "add", kwargs)
        if gate == "refuse":
            return {"refused": "install_policy=never"}
        if gate == "dry":
            return {"dry_run": True, "would": {"action": "update" if update else "add",
                    "name": name, "config": raw},
                    "note": "review with the user, then call again with confirm=true"}
        sc = MCPServerConfig.model_validate(raw)
        method = self._service.update if update else self._service.add
        result = await method(McpServerUpsertCommand(name=name, config=sc), principal)
        return {"name": name, "result": _as_dict(result)}

    async def _install(self, kwargs: dict, principal) -> Any:
        from durin.agent.mcp_install import (
            build_server_config_from_detail,
            collect_secret_env,
            package_runtime,
            runtime_install_command,
            runtime_present,
        )
        from durin.service.mcp import McpServerUpsertCommand

        ref = str(kwargs.get("ref", "")).strip()
        if not ref:
            return {"error": "ref is required"}
        prefer = (str(kwargs.get("prefer", "")).strip() or "remote")
        env_values = kwargs.get("env_values") or {}

        detail = None
        for adapter in build_mcp_adapters(self._registries):
            detail = await adapter.describe(ref)
            if detail is not None:
                break
        if detail is None:
            return {"error": f"server not found in registry: {ref}"}

        server_name = _name_from_ref(ref)
        gate = self._gate("install", kwargs)
        use_local = (prefer == "local" and detail.packages) or (
            not detail.remotes and detail.packages
        )

        runtime_plan: dict | None = None
        if use_local:
            rt = package_runtime(detail.packages[0])
            if not runtime_present(rt):
                cmd = runtime_install_command(rt)
                runtime_plan = {"runtime": rt, "command": cmd,
                                "auto_installable": cmd is not None}

        if gate == "refuse":
            return {"refused": "install_policy=never", "ref": ref,
                    "runtime_plan": runtime_plan}
        if gate == "dry":
            return {"dry_run": True, "ref": ref, "name": server_name,
                    "model": "local" if use_local else "remote",
                    "runtime_plan": runtime_plan,
                    "note": "review with the user, then call again with confirm=true"}

        runtime_note = None
        if runtime_plan and runtime_plan.get("command") and self._exec_run is not None:
            await self._exec_run(command=runtime_plan["command"])
            runtime_note = f"ran: {runtime_plan['command']}"
        elif runtime_plan and not runtime_plan.get("auto_installable"):
            runtime_note = (f"runtime '{runtime_plan['runtime']}' missing and not "
                            "auto-installable — install it manually")

        secret_refs = collect_secret_env(detail, env_values, server_name=server_name)
        sc = build_server_config_from_detail(detail, prefer=prefer,
                                             secret_env_refs=secret_refs)
        from durin.agent.mcp_install import autodetect_oauth

        has_headers = bool(detail.remotes and detail.remotes[0].headers)
        await autodetect_oauth(sc, has_declared_headers=has_headers)
        result = await self._service.add(
            McpServerUpsertCommand(name=server_name, config=sc), principal)
        info = _as_dict(result)
        return {"name": server_name, "result": info, "runtime": runtime_note,
                "needs_oauth": info.get("status") == "needs_auth"}


def _as_dict(result: Any) -> dict:
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, dict):
        return result
    return {"status": getattr(result, "status", None), "ok": getattr(result, "ok", None)}
