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
from durin.session.lineage import ORIGIN_ID, ORIGIN_TYPE, build_lineage, root_of
from durin.session.manager import Session, SessionManager
from durin.workflow.engine import NodeExecutionError, NodeRunRequest, NodeRunResponse
from durin.workflow.persona_resolve import resolve_persona


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


_VERDICT = ("\n\nAfter your assessment, end your reply with a single final line: "
            "'PASS' if the work meets the criteria, or 'FAIL' followed by what to fix.")


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
        app_config=None,
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
        self._app_config = app_config

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

        # Apply persona: prepend its SOUL body and use its model ref when set.
        persona_name = getattr(req.node, "persona", None)
        persona_soul, persona_model_ref = resolve_persona(
            self._app_config, persona_name, self.sessions.workspace)
        if persona_soul:
            system = f"{persona_soul}\n\n{system}" if system else persona_soul

        skills_text = self._load_skills(getattr(req.node, "skills", ()))
        if skills_text:
            system = f"{system}\n\n# Skills\n\n{skills_text}" if system else f"# Skills\n\n{skills_text}"
        # The node's work mode (AgentMode) appends its posture to the prompt so the model
        # adopts the right stance (e.g. read-only in plan/explore).
        from durin.agent.agent_mode import get_mode
        suffix = get_mode(getattr(req.node, "mode", "build")).prompt_suffix
        if suffix:
            system = f"{system}{suffix}" if system else suffix.lstrip()
        if getattr(req.node, "routes", False):
            system = f"{system}{_VERDICT}" if system else _VERDICT.lstrip()
        messages: list[dict] = [{"role": "system", "content": system}]
        messages.extend(req.shared_context)
        user = req.task
        if req.upstream_output:
            user = f"{req.task}\n\n--- Output of the previous step ---\n{req.upstream_output}"
        if getattr(req.node, "tools", "none") == "default" and req.output_dir:
            user = (
                f"{user}\n\n--- Working directory ---\n"
                f"Your working directory for this run is: {req.output_dir}\n"
                "Earlier steps' files are here; create and edit files here so the steps "
                "after you see them."
            )
        messages.append({"role": "user", "content": user})

        # Persona model when a persona is set, else the node's explicit model, else
        # the runner's default. The parser's persona-xor-model guard ensures at most
        # one of persona_model_ref and req.node.model is set at a time.
        model = persona_model_ref or req.node.model or self.default_model

        node_max_turns = getattr(req.node, "max_turns", None)
        if node_max_turns is not None:
            # Prepend a budget note so the model knows to be efficient.
            budget_note = (
                f"\n\nYou have up to {node_max_turns} rounds of tool use. "
                "Gather efficiently, then give your final answer."
            )
            system_msg = messages[0]
            messages[0] = {**system_msg, "content": system_msg["content"] + budget_note}
            run_max_iterations = node_max_turns
        else:
            run_max_iterations = self.max_iterations

        # If the agent turn raises (provider/MCP/tool error), the partial conversation
        # would otherwise be lost and the failure would name no node. Persist whatever
        # messages exist (status node_failed) and raise a typed error carrying the node
        # identity and the persisted session key so the engine can record + name it.
        try:
            result = asyncio.run(self.runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=self._build_tools(req.node, req.workspace_override),
                model=model,
                max_iterations=run_max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
            )))
        except Exception as exc:  # noqa: BLE001 - persist + re-raise as a typed node failure
            raise self._on_failure(req, messages, exc) from exc

        # When the node exhausted its tool-round budget without finishing, make a
        # second call with no tools so the model must emit a synthesis from what it
        # gathered. This converts the canned "max iterations" outcome into a real
        # answer and persists both runs' messages.
        if node_max_turns is not None and result.stop_reason == "max_iterations":
            synthesis_messages = list(result.messages) + [{
                "role": "user",
                "content": (
                    "You have used all your tool rounds. Based solely on what you have "
                    "gathered so far, give your best final answer now."
                ),
            }]
            try:
                synthesis_result = asyncio.run(self.runner.run(AgentRunSpec(
                    initial_messages=synthesis_messages,
                    tools=ToolRegistry(),   # no tools — model must emit text
                    model=model,
                    max_iterations=1,
                    max_tool_result_chars=self.max_tool_result_chars,
                )))
            except Exception as exc:  # noqa: BLE001 - persist the gathered history, then re-raise typed
                raise self._on_failure(req, list(result.messages), exc) from exc
            # synthesis_result.messages is the superset (first-run history + synthesis turns)
            all_messages = synthesis_result.messages
            final_output = synthesis_result.final_content or ""
            final_stop_reason = synthesis_result.stop_reason
        else:
            all_messages = list(result.messages)
            final_output = result.final_content or ""
            final_stop_reason = result.stop_reason

        # A provider that exhausts its retries returns stop_reason="error" with a
        # placeholder string instead of raising. Without this guard the node would be
        # recorded a misleading 'ok' carrying that garbage (and downstream nodes would
        # consume it); treat it as a node failure so it routes exactly like a raised one
        # (sequential -> named abort; parallel worker -> isolated node_failed).
        if final_stop_reason == "error":
            raise self._on_failure(
                req, all_messages,
                RuntimeError(f"model error: {(final_output or 'no response').strip()[:200]}"))

        session_key = self._persist(req, all_messages)
        return NodeRunResponse(
            output=final_output,
            session_key=session_key,
            messages=all_messages,
            # This runner always persists; a None key means the save raised, not that
            # the node had no session — surface that as a persist failure.
            persist_failed=session_key is None,
        )

    def _persist(self, req: NodeRunRequest, messages: list[dict]) -> str | None:
        if req.worker_index is not None:
            key = f"workflow:{req.run_id}:{req.node.id}:{req.iteration}:{req.worker_index}"
        else:
            key = f"workflow:{req.run_id}:{req.node.id}:{req.iteration}"
        try:
            # Headless runs (no calling session) would otherwise self-root each node
            # session at its own key, leaving them as orphans. Root them all under a
            # synthetic per-run session so children_of(run_root) finds every node.
            run_root = req.root_session_key or f"workflow:{req.run_id}:root"
            if not req.root_session_key and not self.sessions.exists(run_root):
                # A top-level run-root: no parent lineage block (so children_of(run_root)
                # returns the node sessions, not the root itself), just an origin marker.
                # Concurrent fan-out workers can both pass this check; save() is an atomic
                # locked replace, so the worst case is a harmless identical re-write.
                stub = Session(key=run_root)
                stub.metadata[ORIGIN_TYPE] = "workflow_run"
                stub.metadata[ORIGIN_ID] = req.run_id
                stub.metadata["title"] = f"workflow run: {req.run_id}"
                self.sessions.save(stub)
            root = root_of(self.sessions.get_or_create(run_root).metadata, default=run_root)
            session = Session(key=key, messages=list(messages))
            session.metadata.update(build_lineage(
                parent_session_id=run_root,
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

    def _on_failure(self, req: NodeRunRequest, messages: list[dict], cause: Exception) -> NodeExecutionError:
        """Persist the node's partial conversation (best-effort) and build the typed
        error the engine catches — so a failed node keeps a navigable session and the
        aborted result can name it by id, iteration and session key."""
        logger.exception("workflow node {} agent turn failed", req.node.id)
        session_key = self._persist(req, messages)
        return NodeExecutionError(req.node.id, req.iteration, session_key, cause)
