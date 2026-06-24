"""The judge runner for parallel branch reconciliation.

For routing verdicts (single-path routing nodes), the node's own agent output is parsed
by ``parse_verdict`` — the judge is not involved. This module keeps only the ``pick``
method used by parallel ``choose`` reconciliation to select the best branch output.
"""

from __future__ import annotations

import asyncio
import re

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.agent.tools.registry import ToolRegistry

_PICK_SYSTEM = (
    "You are a strict reviewer choosing the single best option. Reply with ONLY the "
    "index number of the best option (e.g. '2') on the first line, then a brief reason. "
    "Judge strictly against the criteria."
)


def _parse_index(text: str, n: int) -> int:
    """First integer in the reply, clamped to a valid option index (default 0)."""
    m = re.search(r"\d+", text)
    if not m:
        return 0
    i = int(m.group())
    return i if 0 <= i < n else 0


class AgentJudgeRunner:
    def __init__(self, runner: AgentRunner, *, default_model: str, max_tool_result_chars: int = 16000) -> None:
        self.runner = runner
        self.default_model = default_model
        self.max_tool_result_chars = max_tool_result_chars

    def pick(self, criteria: str, options: list[str], model: str | None) -> int:
        """Choose the best of several branch outputs against the criteria; return its
        index. A fresh reviewer turn (non-equivalent to the producers) decides."""
        if not options:
            return 0
        listing = "\n\n".join(f"[{i}]\n{opt}" for i, opt in enumerate(options))
        messages = [
            {"role": "system", "content": _PICK_SYSTEM},
            {"role": "user", "content": f"Criteria:\n{criteria}\n\nOptions:\n{listing}"},
        ]
        result = asyncio.run(self.runner.run(AgentRunSpec(
            initial_messages=messages,
            tools=ToolRegistry(),
            model=model or self.default_model,
            max_iterations=1,
            max_tool_result_chars=self.max_tool_result_chars,
        )))
        return _parse_index((result.final_content or "").strip(), len(options))
