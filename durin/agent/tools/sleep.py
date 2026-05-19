"""``sleep`` tool — bounded synchronous wait inside a turn.

Lets the model pause the current turn for a fixed delay. The primary use
case is **polling**: the agent kicks off background work (spawn a
subagent, trigger a long-running shell, ask an external system) and then
needs to wait a short while before checking back.

Bounds: 0 to 300 seconds (5 minutes). The cap is intentional —

- A sleep blocks the current turn, holding the LLM streaming connection
  open and consuming the per-turn wall-clock budget. Long waits should
  use ``cron`` instead, which schedules a future re-invocation rather
  than blocking now.
- The cap also prevents prompt-injection style misuse where a malicious
  tool output convinces the agent to sleep indefinitely.

The tool is read-only with respect to the workspace and session
metadata, so it is allowed in every agent mode (plan, explore, build).
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import NumberSchema, StringSchema, tool_parameters_schema
from durin.telemetry.logger import current_telemetry

_MAX_SECONDS = 300.0
_MIN_SECONDS = 0.0


@tool_parameters(
    tool_parameters_schema(
        seconds=NumberSchema(
            description=(
                "How long to wait, in seconds. Must be between 0 and 300 "
                "(5 minutes). For longer waits, schedule a future check "
                "with `cron` instead — `sleep` blocks the current turn."
            ),
            minimum=_MIN_SECONDS,
            maximum=_MAX_SECONDS,
        ),
        reason=StringSchema(
            description=(
                "Optional short note explaining why you are sleeping "
                "(e.g. 'waiting for build to finish'). Recorded in "
                "telemetry; does not change behavior."
            ),
            max_length=200,
            nullable=True,
        ),
        required=["seconds"],
    )
)
class SleepTool(Tool):
    """Block the current turn for *seconds* seconds (0–300)."""

    _scopes = {"core"}

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls()

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return True

    @property
    def name(self) -> str:
        return "sleep"

    @property
    def description(self) -> str:
        return (
            "Pause the current turn for the given number of seconds (max 300). "
            "Use this when you've started background work and need to wait "
            "before polling for results — e.g. after spawning a subagent or "
            "triggering an external job. Do NOT use it as a substitute for "
            "thinking, to 'wait for the user', or to delay obvious next steps. "
            "For long waits (> a few minutes), use `cron` to schedule a future "
            "check instead of blocking this turn."
        )

    async def execute(
        self,
        seconds: float | None = None,
        reason: str | None = None,
        **kwargs: Any,
    ) -> str:
        if seconds is None:
            return "Error: `seconds` is required."
        try:
            requested = float(seconds)
        except (TypeError, ValueError):
            return "Error: `seconds` must be a number."
        if requested < _MIN_SECONDS:
            return f"Error: `seconds` must be >= {_MIN_SECONDS}."
        # Clamp rather than reject: a model that asked for 600s will
        # benefit more from 300s of sleep + clear feedback than from a
        # hard error that may loop it into retrying.
        clamped = min(requested, _MAX_SECONDS)
        was_clamped = clamped < requested

        note = (reason or "").strip()[:200]
        self._emit("sleep.start", {
            "requested_s": requested,
            "actual_s": clamped,
            "clamped": was_clamped,
            "reason": note or None,
        })

        start = time.monotonic()
        try:
            await asyncio.sleep(clamped)
        except asyncio.CancelledError:
            elapsed = time.monotonic() - start
            self._emit("sleep.cancelled", {
                "elapsed_s": elapsed,
                "reason": note or None,
            })
            raise

        elapsed = time.monotonic() - start
        self._emit("sleep.end", {
            "elapsed_s": elapsed,
            "reason": note or None,
        })

        body = f"Slept {elapsed:.2f}s."
        if was_clamped:
            body += (
                f" (Requested {requested:g}s, clamped to the {_MAX_SECONDS:g}s "
                "ceiling — use `cron` for longer waits.)"
            )
        return body

    @staticmethod
    def _emit(event_type: str, data: dict[str, Any]) -> None:
        logger_obj = current_telemetry()
        if logger_obj is None:
            return
        with suppress(Exception):
            logger_obj.log(event_type, data)
