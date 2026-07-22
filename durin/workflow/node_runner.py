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
import json

from loguru import logger

from durin.agent.hook import AgentHook, CompositeHook
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
from durin.workflow.node_progress import NodeCheckpointHook, NodeProgressHook
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

_FINAL_REVIEW = (
    "\n\nThis is the final review round: the producing step has no passes left, so a "
    "FAIL ends the run with the work as-is — there will be no further revision. Give "
    "your definitive verdict: PASS with explicitly noted caveats only if the work is "
    "genuinely acceptable, or FAIL with a clear, final summary of what is missing."
)


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
        # Aux bridge handles (vision/audio/…) are built once per runner —
        # i.e. once per workflow run — and shared by every node's registry,
        # so N nodes don't pay N provider constructions.
        self._aux_providers: dict | None = None

    @staticmethod
    def _pass_note(req: NodeRunRequest) -> str:
        """The loop-awareness note appended to the node's user turn. Empty on a first
        pass (a node that never loops must not be primed to iterate); on a revisit it
        states pass X of Y; on the last allowed pass it says so explicitly so the
        model delivers a final, complete result instead of another increment."""
        if req.budget is None or req.iteration <= 1:
            return ""
        remaining = req.budget - req.iteration
        if remaining > 0:
            return (
                f"\n\n--- Pass {req.iteration} of {req.budget} ---\n"
                f"This step already ran {req.iteration - 1} time(s); after this pass, "
                f"{remaining} more remain(s)."
            )
        return (
            f"\n\n--- FINAL PASS ({req.iteration} of {req.budget}) ---\n"
            "This is this step's last allowed pass; there will be no further iteration. "
            "Deliver your best complete, final result."
        )

    @staticmethod
    def _is_persistent(req: NodeRunRequest) -> bool:
        # Parallel units (worker fan-out / branch forks) always get per-unit fresh
        # sessions; the parser also rejects persistent on them (defense in depth).
        return (getattr(req.node, "session", "fresh") == "persistent"
                and req.worker_index is None and req.workspace_override is None)

    @classmethod
    def _session_key(cls, req: NodeRunRequest) -> str:
        if cls._is_persistent(req):
            return f"workflow:{req.run_id}:{req.node.id}"
        if req.worker_index is not None:
            return f"workflow:{req.run_id}:{req.node.id}:{req.iteration}:{req.worker_index}"
        return f"workflow:{req.run_id}:{req.node.id}:{req.iteration}"

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
                aux_providers=self._get_aux_providers(),
                app_config=self._app_config,
            )
            ToolLoader().load(ctx, registry, scope="subagent")
        self._add_mcp_tools(registry, getattr(node, "mcps", ()))
        return self._apply_mode(node, registry)

    def _get_aux_providers(self) -> dict:
        """Aux bridge handles (vision/audio/…) for node registries, built lazily
        on first use and cached for the runner's lifetime. A build failure only
        hides the bridge tools — it must never break the node."""
        if self._aux_providers is None:
            aux: dict = {}
            if self._app_config is not None:
                from durin.agent.aux_bridges import build_aux_providers
                try:
                    aux = build_aux_providers(self._app_config)
                except Exception:
                    logger.warning("Failed to build aux bridges for workflow nodes; continuing without them")
            self._aux_providers = aux
        return self._aux_providers

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
        if getattr(req, "fail_would_exhaust", False):
            system = f"{system}{_FINAL_REVIEW}"
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
        user = f"{user}{self._pass_note(req)}"
        prior_messages: list[dict] | None = None
        if self._is_persistent(req) and req.iteration > 1:
            try:
                existing = self.sessions.get_or_create(self._session_key(req))
                prior_messages = list(existing.messages) or None
            except Exception:  # noqa: BLE001 - a broken prior session degrades to fresh
                logger.exception("persistent node session reload failed for {}", req.node.id)
        if prior_messages:
            # Resume the node's own conversation: don't rebuild system/task — append a
            # revisit turn carrying only what is NEW (loop feedback + the pass counter).
            revisit = "The flow has returned to this step."
            if req.upstream_output:
                revisit += f"\n\n--- New input for this pass ---\n{req.upstream_output}"
            revisit += self._pass_note(req)
            if getattr(req, "fail_would_exhaust", False):
                revisit += _FINAL_REVIEW
            messages = prior_messages + [{"role": "user", "content": revisit}]
        else:
            messages.append({"role": "user", "content": user})

        # Persona model when a persona is set, else the node's explicit model, else
        # the runner's default. The parser's persona-xor-model guard ensures at most
        # one of persona_model_ref and req.node.model is set at a time.
        model = persona_model_ref or req.node.model or self.default_model

        node_max_turns = getattr(req.node, "max_turns", None)
        if node_max_turns is not None:
            # On a resumed persistent session, append the budget note to the latest
            # revisit turn (the last message) to avoid stacking it on the original
            # system turn which may be persisted and reloaded. On a fresh path,
            # append it to messages[0] (the system turn).
            budget_note = (
                f"\n\nYou have up to {node_max_turns} rounds of tool use. "
                "Gather efficiently, then give your final answer."
            )
            resumed = prior_messages is not None
            if resumed:
                last = messages[-1]
                messages[-1] = {**last, "content": last["content"] + budget_note}
            else:
                system_msg = messages[0]
                messages[0] = {**system_msg, "content": system_msg["content"] + budget_note}
            run_max_iterations = node_max_turns
        else:
            run_max_iterations = self.max_iterations

        # Built only now that run_max_iterations is known, so the progress hook can
        # report the node's actual round budget alongside every round — the same
        # ceiling the agent turn below is bounded by, not the node's separate
        # re-entry budget. The checkpoint hook is unconditional: a node's session
        # must be durable mid-turn whether or not anything is watching its progress.
        # Composing via CompositeHook even for the single-hook case gives the
        # checkpoint hook its error isolation for free (see CompositeHook), so it
        # need not guard its own exceptions. Keep a direct reference to the
        # checkpoint hook (not just the composite) so the failure path below can
        # recover the rounds it already saved.
        hooks: list[AgentHook] = []
        if req.progress is not None:
            hooks.append(NodeProgressHook(req.progress, max_rounds=run_max_iterations))
        checkpoint_hook = NodeCheckpointHook(lambda msgs: self._persist(req, msgs))
        hooks.append(checkpoint_hook)
        hook = CompositeHook(hooks)

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
                # Nodes are read/search-heavy (gather, review, verify): run independent
                # concurrency-safe tool calls in parallel, same as the main loop and
                # subagents; the runner keeps mutations serial.
                concurrent_tools=True,
                hook=hook,
            )))
        except Exception as exc:  # noqa: BLE001 - persist + re-raise as a typed node failure
            # `messages` is the pre-turn snapshot built above. AgentRunner.run()
            # copies it into its own list (`messages = list(spec.initial_messages)`)
            # and mutates only that copy, so this reference never gains the rounds
            # the turn actually completed before raising — the checkpoint hook,
            # wired above, saw that live copy directly instead.
            #
            # Prefer the hook's last checkpoint, but only when it is strictly
            # longer than the pre-turn snapshot rather than merely "the hook fired
            # at all": a length check is a local, self-verifying guarantee that
            # can never replace a longer list with a shorter one, regardless of
            # what AgentRunner.run() does internally — "the hook fired" would
            # instead be trusting that its list only ever grows, which is true
            # today but not a property this call site can verify on its own.
            # `last_persisted` is None when the turn raised before round 1
            # completed; that (and an equal-or-shorter checkpoint) falls back to
            # `messages`, exactly as before this fix.
            checkpointed = checkpoint_hook.last_persisted
            failure_messages = (
                checkpointed if checkpointed is not None and len(checkpointed) > len(messages)
                else messages
            )
            raise self._on_failure(req, failure_messages, exc) from exc

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

        # A routing node's verdict comes from a forced `route` tool call (deterministic: the
        # model must pick one label from this node's own enum), not from parsing free text.
        # On any failure this is None and the engine falls back to text-parse + default.
        route_label = None
        node_cases = getattr(req.node, "cases", None)
        if node_cases:
            route_label = self._derive_route_label(all_messages, list(node_cases.keys()), model)
        elif getattr(req.node, "routes", False):
            route_label = self._derive_route_label(all_messages, ["PASS", "FAIL"], model)

        # The node's OWN contribution: its user turn + everything generated after.
        # The engine extends the shared buffer with this — returning the full
        # conversation here would re-add the system prompt and the inherited
        # shared context, duplicating the buffer on every shared node.
        own_start = max(0, len(messages) - 1)
        own_messages = all_messages[own_start:]

        session_key = self._persist(req, all_messages)
        return NodeRunResponse(
            output=final_output,
            session_key=session_key,
            messages=own_messages,
            # This runner always persists; a None key means the save raised, not that
            # the node had no session — surface that as a persist failure.
            persist_failed=session_key is None,
            route_label=route_label,
        )

    def _derive_route_label(self, messages: list[dict], labels: list[str], model: str | None) -> str | None:
        """Deterministic routing verdict via a forced `route` tool call: the model picks exactly
        one label from this node's enum. Returns the chosen label, or None on any failure (the
        engine then falls back to parsing the node's text output)."""
        tool = {"type": "function", "function": {
            "name": "route",
            "description": "Record your final routing verdict for this step.",
            "parameters": {"type": "object", "properties": {
                "label": {"type": "string", "enum": labels,
                          "description": "Your verdict — exactly one of the allowed values."},
                "reason": {"type": "string", "description": "One short line explaining the verdict."},
            }, "required": ["label"]}}}
        route_messages = list(messages) + [{
            "role": "user",
            "content": ("Record your verdict for this step now by calling the `route` tool with "
                        "exactly one of: " + ", ".join(labels) + "."),
        }]
        try:
            resp = asyncio.run(self.runner.provider.chat(
                messages=route_messages, tools=[tool], tool_choice="required", model=model))
            for tc in (getattr(resp, "tool_calls", None) or []):
                args = getattr(tc, "arguments", None)
                if args is None:
                    args = getattr(tc, "input", None) or getattr(tc, "args", None)
                if isinstance(args, str):
                    args = json.loads(args)
                label = (args or {}).get("label")
                if label in labels:
                    return label
        except Exception:  # noqa: BLE001 - any failure → fall back to text-parse in the engine
            logger.opt(exception=True).debug("route-tool verdict failed; falling back to text parse")
        return None

    def _persist(self, req: NodeRunRequest, messages: list[dict]) -> str | None:
        key = self._session_key(req)
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
        aborted result can name it by id, iteration and session key. ``messages`` is
        whatever the caller resolved as the richest conversation available: the
        checkpoint hook's last mid-turn save when the turn got that far, otherwise
        the pre-turn snapshot."""
        logger.exception("workflow node {} agent turn failed", req.node.id)
        session_key = self._persist(req, messages)
        return NodeExecutionError(req.node.id, req.iteration, session_key, cause)
