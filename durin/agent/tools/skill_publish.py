"""skill_publish tool — promote a draft skill into the active registry."""
from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema


def _DEFAULT_JUDGE(prompt: str) -> str:
    from durin.memory.llm_invoke import judge_llm_invoke
    return judge_llm_invoke(prompt).text


_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the draft skill under skill-drafts/ to publish."),
    override_composition=BooleanSchema(
        description="Skip the composition gate. ONLY when the gate rejected the body, you showed "
        "the user its reason, and the user explicitly said to keep it as prose."),
    required=["name"],
    description=(
        "Publish a draft skill (built and tested under skill-drafts/<name>/) into the active "
        "registry. Runs the composition gate + security scan, stamps provenance, versions it, and "
        "makes it available to search/load. Build and iterate with generic file tools under "
        "skill-drafts/<name>/ first; publish when it works."),
)


@tool_parameters(_PARAMETERS)
class SkillPublishTool(Tool, ContextAware):
    """skill_publish tool."""

    _scopes = {"core"}

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()
        self._session: ContextVar[str | None] = ContextVar("skill_publish_session", default=None)
        self._model: ContextVar[str | None] = ContextVar("skill_publish_model", default=None)

    def set_context(self, ctx: RequestContext) -> None:
        self._session.set(ctx.session_key)
        self._model.set((ctx.metadata or {}).get("model"))

    @property
    def name(self) -> str:
        return "skill_publish"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "SkillPublishTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> str:
        import asyncio

        from durin.agent.skills_store import Attribution, publish_draft_skill

        attribution = Attribution(actor="agent", session=self._session.get(), agent=self._model.get())
        result = await asyncio.to_thread(
            publish_draft_skill,
            self._workspace,
            str(kwargs.get("name", "")),
            attribution=attribution,
            composition_judge=_DEFAULT_JUDGE,
            composition_override=bool(kwargs.get("override_composition")),
        )
        if result.get("composition_rejected"):
            result["hint"] = ("Restructure per the doctrine (delegate orchestration to a workflow or "
                              "bundle deterministic steps as scripts), or retry with "
                              "override_composition=true if the user insists on prose.")
        return json.dumps(result, ensure_ascii=False)
