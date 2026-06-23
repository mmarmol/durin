"""The default node runner: run one real agent turn for a work node and persist
its session with lineage.

Plugs into WorkflowEngine as the ``node_runner``. It is synchronous (the engine
walk is synchronous) and drives the async AgentRunner via asyncio.run on a fresh
event loop per node — fine for the sequential slice. The node's conversation is
persisted as its own session keyed ``workflow:<run_id>:<node_id>:<iteration>`` with
the WS0 lineage block, so node work is navigable, searchable and dream-visible —
exactly like a subagent session. Persistence is best-effort.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.agent.tools.base import Tool
from durin.agent.tools.context import ToolContext
from durin.agent.tools.file_state import FileStates
from durin.agent.tools.loader import ToolLoader
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import ToolsConfig
from durin.session.lineage import build_lineage, root_of
from durin.session.manager import Session, SessionManager
from durin.workflow.engine import NodeRunRequest, NodeRunResponse


class _CrossLoopTool(Tool):
    """Wrap a tool whose async ``execute`` is bound to another event loop — e.g. a
    live MCP connection on the gateway's main loop — so a workflow node running in a
    worker-thread loop can still call it. The call is marshalled onto the owning loop
    (where the MCP session lives) and the result is bridged back. Without this, the
    cross-loop ``await`` raises ('attached to a different loop')."""

    _plugin_discoverable = False

    def __init__(self, inner: Tool, owner_loop) -> None:
        self._inner = inner
        self._owner_loop = owner_loop

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def description(self) -> str:
        return self._inner.description

    @property
    def parameters(self):
        return self._inner.parameters

    async def execute(self, **kwargs):
        import asyncio as _asyncio
        fut = _asyncio.run_coroutine_threadsafe(self._inner.execute(**kwargs), self._owner_loop)
        return await _asyncio.wrap_future(fut)


class AgentNodeRunner:
    def __init__(
        self,
        runner: AgentRunner,
        sessions: SessionManager,
        *,
        default_model: str,
        max_iterations: int = 50,
        max_tool_result_chars: int = 16000,
        tools_config: ToolsConfig | None = None,
        live_tool_registry: ToolRegistry | None = None,
        main_loop=None,
    ) -> None:
        self.runner = runner
        self.sessions = sessions
        self.default_model = default_model
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self._tools_config = tools_config or ToolsConfig()
        # The gateway's live tool registry (carries already-connected MCP tools) and
        # the loop those MCP sessions are bound to — used to give a node a scoped
        # subset of MCP tools without reconnecting (see _CrossLoopTool).
        self._live_tool_registry = live_tool_registry
        self._main_loop = main_loop

    def _build_tools(self, node, workspace_override: str | None = None) -> ToolRegistry:
        """Build the node's tool registry: its built-in set ('none'→empty,
        'default'→the standard subagent tool set) plus a scoped subset of the
        already-connected MCP servers the node selected. ``workspace_override`` points
        the file tools at a private branch copy (writing-in-parallel)."""
        registry = ToolRegistry()
        if getattr(node, "tools", "none") == "default":
            ctx = ToolContext(
                config=self._tools_config,
                workspace=workspace_override or str(self.sessions.workspace.resolve()),
                file_state_store=FileStates(),
                scope="subagent",
            )
            ToolLoader().load(ctx, registry, scope="subagent")
        self._add_mcp_tools(registry, getattr(node, "mcps", ()))
        return self._apply_mode(node, registry)

    def _apply_mode(self, node, registry: ToolRegistry) -> ToolRegistry:
        """Restrict the registry to what the node's work mode (AgentMode) allows — e.g.
        a 'plan'/'explore' node is read-only, 'build' (default) keeps everything."""
        from durin.agent.agent_mode import get_mode
        mode = get_mode(getattr(node, "mode", "build"))
        if mode.allowed is None and not mode.denied:
            return registry   # build / unrestricted — no filtering
        filtered = ToolRegistry()
        for name in registry.tool_names:
            if mode.is_tool_allowed(name):
                filtered.register(registry.get(name))
        return filtered

    def _add_mcp_tools(self, registry: ToolRegistry, servers) -> None:
        """Register the live MCP tools of the selected servers into this node's
        registry (by reference, wrapped for cross-loop execution). Reuses the
        gateway's existing connections — no per-node reconnection."""
        # Without a real owner loop there is nowhere to marshal MCP calls to, so the
        # wrapped tool would fail at call time — skip MCP rather than hand out broken tools.
        if not servers or self._live_tool_registry is None or self._main_loop is None:
            return
        for server in servers:
            prefix = f"mcp_{server}_"
            for tool_name in self._live_tool_registry.tool_names:
                if tool_name.startswith(prefix):
                    inner = self._live_tool_registry.get(tool_name)
                    if inner is not None:
                        registry.register(_CrossLoopTool(inner, self._main_loop))

    def _load_skills(self, names) -> str:
        """Load the named skills' content for injection into this node's prompt,
        reusing the same loader the main agent uses so a skill reads identically."""
        if not names:
            return ""
        from durin.agent.skills import SkillsLoader
        return SkillsLoader(self.sessions.workspace).load_skills_for_context(list(names))

    def __call__(self, req: NodeRunRequest) -> NodeRunResponse:
        system = req.node.prompt
        skills_text = self._load_skills(getattr(req.node, "skills", ()))
        if skills_text:
            system = f"{system}\n\n# Skills\n\n{skills_text}" if system else f"# Skills\n\n{skills_text}"
        # The node's work mode (AgentMode) appends its posture to the prompt so the model
        # adopts the right stance (e.g. read-only in plan/explore).
        from durin.agent.agent_mode import get_mode
        suffix = get_mode(getattr(req.node, "mode", "build")).prompt_suffix
        if suffix:
            system = f"{system}{suffix}" if system else suffix.lstrip()
        messages: list[dict] = [{"role": "system", "content": system}]
        messages.extend(req.shared_context)
        user = req.task
        if req.upstream_output:
            user = f"{req.task}\n\n--- Output of the previous step ---\n{req.upstream_output}"
        messages.append({"role": "user", "content": user})

        result = asyncio.run(self.runner.run(AgentRunSpec(
            initial_messages=messages,
            tools=self._build_tools(req.node, req.workspace_override),
            model=req.node.model or self.default_model,
            max_iterations=self.max_iterations,
            max_tool_result_chars=self.max_tool_result_chars,
        )))

        session_key = self._persist(req, result.messages)
        return NodeRunResponse(
            output=result.final_content or "",
            session_key=session_key,
            messages=list(result.messages),
        )

    def _persist(self, req: NodeRunRequest, messages: list[dict]) -> str | None:
        key = f"workflow:{req.run_id}:{req.node.id}:{req.iteration}"
        try:
            parent = req.root_session_key
            root = (
                root_of(self.sessions.get_or_create(parent).metadata, default=parent)
                if parent else key
            )
            session = Session(key=key, messages=list(messages))
            session.metadata.update(build_lineage(
                parent_session_id=parent or key,
                root_id=root,
                origin_type="workflow_node",
                origin_id=f"{req.run_id}:{req.node.id}:{req.iteration}",
            ))
            session.metadata["title"] = f"workflow node: {req.node.id}"
            self.sessions.save(session)
            return key
        except Exception:
            logger.exception("workflow node session persist failed for {}", key)
            return None
