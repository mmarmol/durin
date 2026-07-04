"""skill_write tool — the sanctioned skill-authoring tool.

Auto-discovered into the main agent's ``core`` toolset (like E1's
``skill_edit``), so it is available in-loop as well as to the 2h dream. It
routes through E1's service layer (:func:`dream_create_skill`) instead of a raw
WriteFileTool, so every authored skill is a first-class citizen: provenance
source='dream', mode='auto', and committed to the skills subtree. Optional
bundled ``files`` (scripts, references) go through the same security scan
imports get before the skill activates.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

logger = logging.getLogger(__name__)


def _DEFAULT_JUDGE(prompt: str) -> str:
    """Composition-gate judge: one completion via the judge aux preset
    (``skills.security.llm_judge.model`` → the user's default preset).
    Loop-safe sync invoke; exceptions propagate and the gate accepts
    (failure-open)."""
    from durin.memory.llm_invoke import judge_llm_invoke
    return judge_llm_invoke(prompt).text


_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the new skill (directory name)."),
    content=StringSchema("Full SKILL.md body to author for the new skill."),
    rationale=StringSchema(
        "Why this skill is worth creating — recorded as the commit message. "
        "Use for a recurring pattern that no existing skill covers."
    ),
    files=ArraySchema(
        ObjectSchema(
            path=StringSchema("Relative path inside the skill dir (e.g. scripts/convert.py)."),
            content=StringSchema("The file's full content."),
            required=["path", "content"],
        ),
        description=(
            "Optional bundled files (scripts, references) written next to SKILL.md. "
            "Prefer a script for any deterministic step. Bundling code sends the "
            "skill through the security scan before it activates; a risky verdict "
            "quarantines it for review instead of installing."
        ),
    ),
    override_composition=BooleanSchema(
        description=(
            "Skip the composition gate. ONLY when the gate rejected this body, you "
            "showed the user its reason, and the user explicitly said to keep it as "
            "prose anyway — their word wins. Never set it on your own judgment."
        ),
    ),
    required=["name", "content", "rationale"],
    description=(
        "Create a new skill (a step-by-step procedure to follow later). Writes "
        "skills/<name>/SKILL.md through the sanctioned store: versioned, committed, and "
        "attributed to whoever calls it (you in-session, or the dream). Include YAML "
        "frontmatter with `name` and `description` — the description is the only text "
        "used to decide when the skill surfaces, so state its trigger conditions. Use "
        "for a recurring pattern no existing skill covers; search first. Compose per "
        "the doctrine: deterministic steps as bundled `files` scripts, orchestration "
        "delegated to a workflow (see `list_workflows` / `workflow_write`), prose only "
        "for knowledge and judgment. A prose body that narrates a workflow-shaped "
        "procedure is rejected by a composition gate with the reason."
    ),
)


@tool_parameters(_PARAMETERS)
class SkillWriteTool(Tool, ContextAware):
    """skill_write tool — the sanctioned skill-authoring tool.

    Used by the dream and available in-loop; routes through skills_store.
    """

    def __init__(self, workspace: str | Path, *, gate_mode: str = "override",
                 composition_judge=_DEFAULT_JUDGE) -> None:
        # gate_mode: "override" (in-session — the user's explicit word may skip
        # the composition gate) or "hard" (autonomous dream — no override).
        self._workspace = Path(workspace).expanduser()
        self._gate_mode = gate_mode
        self._composition_judge = composition_judge
        self._session: ContextVar[str | None] = ContextVar("skill_write_session", default=None)
        self._model: ContextVar[str | None] = ContextVar("skill_write_model", default=None)

    def set_context(self, ctx: RequestContext) -> None:
        self._session.set(ctx.session_key)
        self._model.set((ctx.metadata or {}).get("model"))

    @property
    def name(self) -> str:
        return "skill_write"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "SkillWriteTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> str:
        from durin.agent.skills_store import Attribution, dream_create_skill

        raw_files = kwargs.get("files") or []
        if not isinstance(raw_files, list):
            return json.dumps({"error": "files must be an array of {path, content}"})
        files: dict[str, str] = {}
        for entry in raw_files:
            if not isinstance(entry, dict) or "path" not in entry or "content" not in entry:
                return json.dumps({"error": "each files entry needs path and content"})
            files[str(entry["path"])] = str(entry["content"])

        # The user's explicit word may skip the gate in-session; the dream's
        # instance runs gate_mode="hard" and ignores the override outright.
        override = bool(kwargs.get("override_composition")) and self._gate_mode == "override"

        attribution = Attribution(actor="agent", session=self._session.get(), agent=self._model.get())
        result = dream_create_skill(
            self._workspace,
            str(kwargs.get("name", "")),
            str(kwargs.get("content", "")),
            str(kwargs.get("rationale", "")),
            attribution=attribution,
            files=files,
            composition_judge=self._composition_judge,
            composition_override=override,
        )
        if result.get("composition_rejected"):
            result["hint"] = (
                "Restructure per the doctrine: delegate the orchestration to a "
                "workflow (list_workflows / workflow_write) or bundle the "
                "deterministic steps as `files` scripts, keeping only domain "
                "knowledge and judgment as prose."
                + (" If the user explicitly insists on prose after seeing this "
                   "reason, retry with override_composition=true."
                   if self._gate_mode == "override" else "")
            )
            if self._gate_mode == "hard":
                # The autonomous door has no override — the bounced body goes to
                # the suggestions bandeja, annotated, so the captured procedure
                # doesn't die with this subagent turn. A later compliant landing
                # of the same name clears the stale card (below).
                try:
                    from durin.agent.skill_suggestions import add_gate_bounce
                    add_gate_bounce(
                        self._workspace,
                        name=str(kwargs.get("name", "")),
                        content=str(kwargs.get("content", "")),
                        gate_reason=str(result.get("error", "")).removeprefix("composition gate: "),
                    )
                    result["note"] = "queued for the user's review in the suggestions bandeja"
                except Exception:  # noqa: BLE001 - the bandeja must never break authoring
                    logger.exception("failed to queue gate bounce for %s", kwargs.get("name"))
        elif result.get("ok") and self._gate_mode == "hard":
            try:
                from durin.agent.skill_suggestions import clear_gate_bounces
                clear_gate_bounces(self._workspace, str(result.get("name", "")))
            except Exception:  # noqa: BLE001
                logger.exception("failed to clear stale gate bounces for %s", result.get("name"))
        return json.dumps(result, ensure_ascii=False)
