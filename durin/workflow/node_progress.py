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


class NodeProgressHook(AgentHook):
    __slots__ = ("_emit",)

    def __init__(self, emit: Callable[[dict], None]) -> None:
        super().__init__()
        self._emit = emit

    def _send(self, payload: dict[str, Any]) -> None:
        try:
            self._emit(payload)
        except Exception:  # noqa: BLE001 - a dead listener must not fail the node
            logger.debug("workflow node progress emit failed (suppressed)")

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        call = context.tool_calls[0] if context.tool_calls else None
        activity = None
        if call is not None:
            activity = {
                "tool": call.name,
                "target": tool_target(call.arguments),
                "at": time.time(),
            }
        self._send({"round": context.iteration, "activity": activity})

    async def after_iteration(self, context: AgentHookContext) -> None:
        self._send({"round": context.iteration, "activity": None})
