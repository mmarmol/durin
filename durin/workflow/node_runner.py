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
from durin.agent.tools.context import ToolContext
from durin.agent.tools.file_state import FileStates
from durin.agent.tools.loader import ToolLoader
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import ToolsConfig
from durin.session.lineage import build_lineage, root_of
from durin.session.manager import Session, SessionManager
from durin.workflow.engine import NodeRunRequest, NodeRunResponse


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
    ) -> None:
        self.runner = runner
        self.sessions = sessions
        self.default_model = default_model
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self._tools_config = tools_config or ToolsConfig()

    def _build_tools(self, node) -> ToolRegistry:
        """Build the node's tool registry. 'none' → empty; 'default' → the
        standard tool set, loaded the same way a subagent's tools are."""
        registry = ToolRegistry()
        if getattr(node, "tools", "none") != "default":
            return registry
        ctx = ToolContext(
            config=self._tools_config,
            workspace=str(self.sessions.workspace.resolve()),
            file_state_store=FileStates(),
            scope="subagent",
        )
        ToolLoader().load(ctx, registry, scope="subagent")
        return registry

    def __call__(self, req: NodeRunRequest) -> NodeRunResponse:
        messages: list[dict] = [{"role": "system", "content": req.node.prompt}]
        messages.extend(req.shared_context)
        user = req.task
        if req.upstream_output:
            user = f"{req.task}\n\n--- Output of the previous step ---\n{req.upstream_output}"
        messages.append({"role": "user", "content": user})

        result = asyncio.run(self.runner.run(AgentRunSpec(
            initial_messages=messages,
            tools=self._build_tools(req.node),
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
