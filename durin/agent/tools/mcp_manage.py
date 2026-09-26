"""mcp_manage tool — create/modify/install MCP servers behind an approval.

The single WRITE counterpart to ``mcp_search``. Wraps ``McpService`` (add / update /
remove / enable / disable / reconnect) and the registry install path, so the agent can
act on a user's conversational request ("add an MCP at this URL", "raise that timeout",
"remove it", "install the jira server we found").

Gating: add, update, install and enable put a server's command or endpoint into the
agent's tool surface, and an injected prompt could try to slip a malicious server in.
They honour ``tools.mcp_discovery.install_policy``: ``never`` refuses, ``auto`` runs
(the operator granted it in config ahead of time), and ``approve`` (default) files an
approval request that the person in the chat approves or rejects; with nobody to ask
it waits for approval (``durin approvals``). Nothing in the call arguments can approve
it — a ``confirm`` a model still sends is never read. Secrets are always supplied by
the human (OAuth login, ``request_secret``, the dashboard): a credential in a server
config must be a ``${secret:NAME}`` reference. Runtime install (e.g. ``brew install
node`` for a local server whose runtime is missing) runs through the exec tool's
non-asking entry point, as part of the approved install — it must never open a
second, nested approval mid-turn.
"""
from __future__ import annotations

from typing import Any

from durin.agent import approval
from durin.agent import approval_kinds_mcp as mcp_kind
from durin.agent.approval_executors import ExecDeps, Prepared
from durin.agent.approval_prompt import ChatHandles
from durin.agent.mcp_registry import build_mcp_adapters
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext, RequestContextVar
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

# Changes that put a server's command or endpoint into the agent's tool surface.
# enable is one of them: it starts a switched-off server's process or connection again.
_GATED = {"install", "add", "update", "enable"}
# These add no executable state: remove and disable take a server away.
# reconnect is a special case — it has its own check (see _reconnect) instead
# of the approval channel: it refuses instead of connecting whenever the
# on-disk config doesn't match McpService.approved_config, so THIS action can
# only ever repeat a config a person or an approved request already put in
# place, never pick up a new one — a config change has to go through
# `update`/`enable` (gated) or the person's own dashboard reconnect. A shell
# command run via exec could still overwrite config.json directly; guarding
# that is exec's own sandbox's job (tools.exec.sandbox / deny_patterns), out
# of scope here. They stay ungated (in the approval-channel sense).
_UNGATED = {"remove", "disable", "reconnect"}

_PARAMETERS = tool_parameters_schema(
    action=StringSchema(
        "One of: install (from a registry ref), add (explicit config), update, remove, "
        "enable, disable, reconnect."
    ),
    ref=StringSchema("Registry ref for action=install (from mcp_search)."),
    name=StringSchema("Server name for add/update/remove/enable/disable/reconnect."),
    config=StringSchema(
        "JSON object of MCPServerConfig fields for action=add/update. A credential "
        "value must be a whole ${secret:NAME} reference (use request_secret first)."
    ),
    prefer=StringSchema("For install: 'remote' (default) or 'local'."),
    description=(
        "Create, modify, install, or remove an MCP server. Discover refs first with "
        "mcp_search. install/add/update/enable need the user's approval "
        "(install_policy=approve): in a chat the user is asked and the call returns "
        "their answer; otherwise it waits for approval (the dashboard's Pending "
        "page, or `durin approvals`). Remote "
        "installs hand off to a human OAuth login; secrets are entered by the human, "
        "never the agent."
    ),
)


@tool_parameters(_PARAMETERS)
class McpManageTool(Tool, ContextAware):
    """mcp_manage tool — gated MCP server CRUD + registry install."""

    def __init__(self, *, service, exec_run=None, install_policy="approve",
                 registries=None, workspace=".", sessions=None, bus=None,
                 approval_timeout_s: float = 300.0) -> None:
        self._service = service
        self._exec_run = exec_run
        self._policy = install_policy
        self._registries = list(registries or [])
        self._workspace = workspace
        self._chat = ChatHandles(sessions=sessions, bus=bus, timeout_s=approval_timeout_s)
        # This turn's context: the instance is shared by concurrent turns.
        self._ctx = RequestContextVar("mcp_manage_request_ctx")

    def set_context(self, ctx: RequestContext) -> None:
        self._ctx.set(ctx)

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
        chat = ChatHandles.from_tool_context(ctx)
        return cls(
            service=McpService(mcp_runtime=runtime),
            # The non-asking entry point: a runtime-install step that hits the
            # deny list fails the install with the refusal text instead of
            # opening a second approval nested inside this one.
            exec_run=ExecTool.create(ctx)._run,
            install_policy=disc.install_policy,
            registries=list(disc.registries),
            workspace=getattr(ctx, "workspace", "."),
            sessions=chat.sessions,
            bus=chat.bus,
            approval_timeout_s=chat.timeout_s,
        )

    def _session_key(self) -> str | None:
        ctx = self._ctx.get()
        return ctx.session_key if ctx is not None else None

    async def _submit(self, prepared: Prepared) -> dict:
        """Run *prepared* now under install_policy=auto; otherwise ask for approval."""
        deps = ExecDeps(mcp=self._service, exec_run=self._exec_run)
        if self._policy == "auto":
            # install_policy=auto IS pre-declared authority: the operator granted it
            # in config, out of band, before the run — so it holds with nobody
            # watching.
            return await mcp_kind.apply(prepared.payload, deps)
        # The person in this chat decides. With nobody to ask (cron, workflow,
        # sub-agent, no live consumer, or a turn with input from an API token)
        # the asker is None and the request waits for `durin approvals`.
        ask = self._chat.asker(self._ctx.get())
        outcome = await approval.request(self._workspace, prepared,
                                         session_key=self._session_key(),
                                         deps=deps, ask=ask)
        return approval.outcome_to_tool_result(outcome)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.service.principal import Principal

        action = str(kwargs.get("action", "")).strip()
        try:
            if action in _GATED and self._policy == "never":
                return {"refused": "install_policy=never", "action": action}
            if action == "install":
                return await self._install(kwargs)
            if action in ("add", "update"):
                return await self._upsert(action, kwargs)
            if action == "enable":
                name = str(kwargs.get("name", "")).strip()
                if not name:
                    return {"error": "name is required"}
                return await self._submit(mcp_kind.prepare_enable(name))
            if action == "reconnect":
                return await self._reconnect(kwargs)
            if action in _UNGATED:
                return await self._name_action(action, kwargs, Principal.local())
            return {"error": f"unknown action: {action!r}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    async def _reconnect(self, kwargs: dict) -> Any:
        """The agent's own reconnect: unlike the person's dashboard/REST
        reconnect (``McpService.reconnect``, which always applies whatever is
        on disk — that IS the point of a person clicking Reconnect), an
        unattended agent action has no standing to legitimize a config nobody
        here approved. It refuses instead of connecting unless the on-disk
        entry is exactly the config a person or an approved request already
        put in place (``McpService.approved_config``) — so it can only ever
        repeat what is already running, never pick up a new command or
        endpoint that slipped into config.json some other way.
        """
        from durin.config.loader import load_config
        from durin.service.mcp import McpServerNameCommand
        from durin.service.principal import Principal

        name = str(kwargs.get("name", "")).strip()
        if not name:
            return {"error": "name is required"}
        sc = load_config().tools.mcp_servers.get(name)
        if sc is None:
            return {"error": f"no such MCP server: {name!r}"}
        approved = self._service.approved_config(name)
        if approved is None or approved.model_dump(mode="json") != sc.model_dump(mode="json"):
            return {"error": (
                f"{name!r} changed outside durin since it was last approved; ask the "
                "person to reconnect it from the dashboard, or use `update`"
            )}
        result = await self._service.reconnect(McpServerNameCommand(name=name), Principal.local())
        return {"name": name, "result": mcp_kind.as_dict(result)}

    async def _name_action(self, action: str, kwargs: dict, principal) -> Any:
        from durin.service.mcp import McpServerNameCommand

        name = str(kwargs.get("name", "")).strip()
        if not name:
            return {"error": "name is required"}
        method = getattr(self._service, action)
        result = await method(McpServerNameCommand(name=name), principal)
        return {"name": name, "result": mcp_kind.as_dict(result)}

    async def _upsert(self, action: str, kwargs: dict) -> Any:
        import json

        name = str(kwargs.get("name", "")).strip()
        raw = kwargs.get("config") or {}
        if isinstance(raw, str):
            raw = json.loads(raw) if raw.strip() else {}
        if not name:
            return {"error": "name is required"}
        return await self._submit(mcp_kind.prepare_upsert(action, name, raw))

    async def _install(self, kwargs: dict) -> Any:
        ref = str(kwargs.get("ref", "")).strip()
        if not ref:
            return {"error": "ref is required"}
        prefer = (str(kwargs.get("prefer", "")).strip() or "remote")
        detail = None
        for adapter in build_mcp_adapters(self._registries):
            detail = await adapter.describe(ref)
            if detail is not None:
                break
        if detail is None:
            return {"error": f"server not found in registry: {ref}"}
        return await self._submit(
            await mcp_kind.prepare_install(detail, ref=ref, prefer=prefer))
