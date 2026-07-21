"""The hook that makes a running workflow node legible from outside.

A node is a full agent turn: it can run for minutes between the engine's
"node started" and "node finished" frames. This hook reports from inside that
turn — which round it is on, and which tool it is about to invoke.

It reports from ``before_execute_tools`` rather than ``after_iteration``: the
former names the tool that is about to run, the latter names the one that
already finished. For a six-minute node the difference is the whole point.

``emit`` is synchronous by contract. The node executes on a worker thread
inside its own event loop, so the callback must marshal to the gateway's loop
rather than await it.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from loguru import logger

from durin.agent.hook import AgentHook, AgentHookContext
from durin.workflow.progress import tool_target


def _round(context: AgentHookContext) -> int:
    """The node's current agent round, 1-based.

    The runner counts iterations from zero (`for iteration in range(...)`), but
    the sibling `iteration` field in the same progress frame is 1-based and
    renders as "pass X of Y". Normalizing here keeps the two comparable and
    stops any surface from showing "round 0 of 10".
    """
    return context.iteration + 1


class NodeProgressHook(AgentHook):
    __slots__ = ("_emit", "_max_rounds")

    def __init__(self, emit: Callable[[dict], None], max_rounds: int | None = None) -> None:
        super().__init__()
        self._emit = emit
        self._max_rounds = max_rounds

    def _send(self, build: Callable[[], dict[str, Any]]) -> None:
        """Build and emit a payload under one guard. The runner calls the hook's
        methods directly with no try/except of its own, so a raise from building
        the payload is exactly as dangerous to the node as a raise from a dead
        listener — both must be swallowed here, not just the emit call."""
        try:
            self._emit(build())
        except Exception:  # noqa: BLE001 - a dead listener or a bad payload must not fail the node
            logger.opt(exception=True).debug("workflow node progress emit failed (suppressed)")

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        def _build() -> dict[str, Any]:
            call = context.tool_calls[0] if context.tool_calls else None
            activity = None
            if call is not None:
                activity = {
                    "tool": call.name,
                    "target": tool_target(call.arguments),
                    "at": time.time(),
                }
            return {"round": _round(context), "activity": activity, "max_rounds": self._max_rounds}
        self._send(_build)

    async def after_iteration(self, context: AgentHookContext) -> None:
        self._send(lambda: {"round": _round(context), "activity": None, "max_rounds": self._max_rounds})


class NodeCheckpointHook(AgentHook):
    """Persist a node's conversation every round, not only when its turn returns.

    Without this a node interrupted mid-turn — a gateway restart, an OOM kill —
    loses every round it completed, and a hung node leaves no transcript to
    inspect. The main agent loop and sub-agents already checkpoint mid-turn;
    this gives workflow nodes the same durability.

    ``context.messages`` is the runner's own live list — the same object it
    mutates in place across iterations (see AgentRunner.run) — so every call
    reflects everything accumulated up to that round, never a snapshot frozen
    at an earlier one.

    This hook does not guard its own exceptions: it must always be composed
    inside a ``CompositeHook`` (see durin/agent/hook.py), whose per-hook error
    isolation is what keeps a failing persist from ever aborting the node.
    """

    __slots__ = ("_persist",)

    def __init__(self, persist: Callable[[list[dict]], None]) -> None:
        super().__init__()
        self._persist = persist

    async def after_iteration(self, context: AgentHookContext) -> None:
        self._persist(context.messages)
