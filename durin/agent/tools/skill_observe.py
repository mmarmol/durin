"""skill_observe tool — log live skill feedback to the observation queue.

Auto-discovered into the main agent's ``core`` toolset. Log, don't act: the
record lands in the observation store (committed to the skills subtree) and
the daily curation pass — not this tool — decides whether a skill changes.
The structural trigger lives in the identity template's skills section.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import RequestContext
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    skill=StringSchema(
        "Affected skill name; 'new:<working-name>' for a gap no skill covers; "
        "'all' for a lesson that applies across skills."
    ),
    kind=StringSchema(
        "One of: correction (user corrected output produced while the skill "
        "was loaded), gap (no skill covers a recurring procedure), improvement "
        "(a better approach emerged than the skill documents), simplify (a "
        "skill rule/section proved dead weight or counterproductive)."
    ),
    issue=StringSchema(
        "What happened — specific enough to act on weeks later without this "
        "conversation."
    ),
    improvement=StringSchema(
        "Concrete suggested change. For existing skills, reference the "
        "section or rule; for new skills, the scope."
    ),
    principle=StringSchema(
        "Optional: the generalizable takeaway, if this matters beyond the "
        "specific instance."
    ),
    required=["skill", "kind", "issue", "improvement"],
    description=(
        "Silently log a skill observation — a user correction, coverage gap, "
        "improvement, or pruning signal — to the persistent queue the daily "
        "curation reviews. Log, don't act: never edit the skill in the same "
        "turn; duplicates of an open observation are merged automatically."
    ),
)


@tool_parameters(_PARAMETERS)
class SkillObserveTool(Tool):
    """skill_observe tool — append one observation to the queue."""

    def __init__(self, workspace: str | Path, session_key: str | None = None) -> None:
        self._workspace = Path(workspace).expanduser()
        # The in-loop registry is built once and serves every session, so the
        # current session arrives per request via set_context (ContextAware);
        # the constructor value is the fallback for direct construction
        # (sub-agents, tests).
        self._session_key: ContextVar[str | None] = ContextVar(
            "skill_observe_session_key", default=session_key)

    @property
    def name(self) -> str:
        return "skill_observe"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "SkillObserveTool":
        return cls(workspace=ctx.workspace,
                   session_key=getattr(ctx, "session_key", None))

    def set_context(self, ctx: RequestContext) -> None:
        """Bind the current request's session for observation provenance."""
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")

    async def execute(self, **kwargs: Any) -> str:
        from durin.agent.skill_observations import log_observation

        result = log_observation(
            self._workspace,
            skill=str(kwargs.get("skill", "")),
            kind=str(kwargs.get("kind", "")),
            issue=str(kwargs.get("issue", "")),
            improvement=str(kwargs.get("improvement", "")),
            principle=(str(kwargs["principle"]) if kwargs.get("principle") else None),
            session=self._session_key.get(),
        )
        return json.dumps(result, ensure_ascii=False)
