"""The judge runner: a reviewer agent evaluates a node's output against criteria.

A judgment decision node routes on this verdict. The judge runs as one AgentRunner
turn on a FRESH context (it sees only the criteria + the work, not the producer's
conversation) with a reviewer role — so it is never *structurally equivalent* to the
producer that made the work (it varies role and context; an optional ``judge_model``
adds the capability axis). The judge replies with PASS or FAIL on the first line; the
full reply is kept as feedback and, on FAIL, threaded back to the producer on loop-back.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.agent.tools.registry import ToolRegistry

_SYSTEM = (
    "You are a strict reviewer. Evaluate the work below against the criteria. "
    "Reply with 'PASS' on the first line if it fully meets the criteria, or 'FAIL' "
    "on the first line if it does not. After that line, give a brief, concrete reason "
    "(on FAIL, say exactly what to fix)."
)

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


@dataclass
class JudgeVerdict:
    passed: bool
    feedback: str


class AgentJudgeRunner:
    def __init__(self, runner: AgentRunner, *, default_model: str, max_tool_result_chars: int = 16000) -> None:
        self.runner = runner
        self.default_model = default_model
        self.max_tool_result_chars = max_tool_result_chars

    def __call__(self, criteria: str, output: str, model: str | None) -> JudgeVerdict:
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"Criteria:\n{criteria}\n\nWork to review:\n{output}"},
        ]
        result = asyncio.run(self.runner.run(AgentRunSpec(
            initial_messages=messages,
            tools=ToolRegistry(),
            model=model or self.default_model,
            max_iterations=1,
            max_tool_result_chars=self.max_tool_result_chars,
        )))
        text = (result.final_content or "").strip()
        passed = text.upper().startswith("PASS")
        return JudgeVerdict(passed=passed, feedback=text)

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
