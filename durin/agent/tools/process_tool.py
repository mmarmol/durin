"""Process tool: manage background processes started by exec(background=true)."""

from __future__ import annotations

import json
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.process_registry import get_process_registry
from durin.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "Action to perform: list | poll | kill",
            enum=["list", "poll", "kill"],
        ),
        id=StringSchema("Process id (proc_...) — required for poll and kill"),
        force=BooleanSchema(description="kill: send SIGKILL immediately (default false)"),
        tail_chars=IntegerSchema(
            2000,
            description="poll: how many chars of output tail to return (default 2000)",
            minimum=100,
            maximum=10000,
        ),
        required=["action"],
    )
)
class ProcessTool(Tool):
    """List, poll and kill background processes."""

    _scopes = {"core", "subagent"}

    @property
    def name(self) -> str:
        return "process"

    @property
    def description(self) -> str:
        return (
            "Manage background processes started with exec(background=true). "
            "list: all tracked processes; poll: status + output tail of one; "
            "kill: terminate one (its whole process group). Poll periodically "
            "(combine with the sleep tool) instead of busy-looping."
        )

    async def execute(
        self, action: str | None = None, id: str | None = None,
        force: bool = False, tail_chars: int = 2000, **kwargs: Any,
    ) -> str:
        reg = get_process_registry()
        if action == "list":
            entries = reg.list_sessions()
            if not entries:
                return "No background processes (running or recently finished)."
            return json.dumps(entries, indent=2)
        if action == "poll":
            if not id:
                return "Error: 'id' is required for poll"
            info = reg.poll(id, tail_chars=tail_chars)
            if "error" in info:
                return f"Error: {info['error']}"
            tail = info.pop("output_tail", "")
            lines = json.dumps(info, indent=2)
            return f"{lines}\n\n--- output tail ---\n{tail}" if tail else lines
        if action == "kill":
            if not id:
                return "Error: 'id' is required for kill"
            result = await reg.kill(id, force=force)
            if not result.get("killed"):
                return f"Error: {result.get('error', 'kill failed')}"
            return f"Killed background process {id} (process group terminated)."
        return f"Error: unknown action '{action}' (use list | poll | kill)"
