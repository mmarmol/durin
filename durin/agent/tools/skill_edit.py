"""skill_edit tool — evolve a skill in-loop with a versioned, rationale'd edit.

An edit to an ``auto`` skill whose security scan needs no review lands at once.
Everything else is decided by the server, never by a tool argument: an edit to
a ``manual`` skill (its owner consents) and an edit that makes the scan worse
or adds findings are shown to the person in this chat, or filed in Pending when
nobody can answer; the configured skills judge may clear an ``auto`` skill's
``caution`` edit on its own.
"""
from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from durin.agent import approval
from durin.agent import approval_kinds_skills as kinds
from durin.agent.approval_executors import ExecDeps
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the skill to edit (directory name)."),
    old=StringSchema(
        "Exact text to replace. Must be unique in the file. Use an empty "
        "string to append `new` to the end (or create the file)."
    ),
    new=StringSchema("Replacement text."),
    rationale=StringSchema(
        "Why this change improves the skill — recorded as the commit message. "
        "Prefer native durin tools over generic ones and automate repetition."
    ),
    file=StringSchema(
        "File within the skill dir to edit. Defaults to 'SKILL.md'. May target "
        "a script under scripts/."
    ),
    required=["name", "old", "new", "rationale"],
    description=(
        "Edit one of durin's own skills and version the change (reversible). "
        "Use when, mid-task, you discover a better approach than a skill "
        "describes, or a skill has a bug/pitfall worth recording. Editing a "
        "builtin forks it into the workspace first. A manual skill's edit, or "
        "one that makes its security scan worse, is shown to the person for "
        "approval by the tool itself — or waits in `durin approvals` when "
        "nobody can answer now. If the result is pending or rejected, "
        "continue without it; do not retry it and do not reach the same "
        "effect another way."
    ),
)


@tool_parameters(_PARAMETERS)
class SkillEditTool(Tool, ContextAware):
    """skill_edit tool."""

    # Core-only: self-modification of skills stays a primary-agent decision.
    _scopes = {"core"}

    def __init__(self, workspace: str | Path, *, chat: kinds.ChatHandles | None = None,
                 judge: tuple[str, str, str] | None = None) -> None:
        self._workspace = Path(workspace).expanduser()
        self._chat = chat or kinds.ChatHandles()
        self._judge = judge or ("off", "", "caution")
        self._ctx: ContextVar[RequestContext | None] = ContextVar("skill_edit_ctx", default=None)

    def set_context(self, ctx: RequestContext) -> None:
        self._ctx.set(ctx)

    @property
    def name(self) -> str:
        return "skill_edit"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "SkillEditTool":
        return cls(workspace=ctx.workspace, chat=kinds.ChatHandles.from_tool_context(ctx),
                   judge=kinds.judge_settings(getattr(ctx, "app_config", None)))

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skills_store import (
            Attribution,
            gate_skill_edit,
            scan_skill_write,
            write_skill_edit,
        )

        name = str(kwargs.get("name", "")).strip()
        if not name:
            return {"error": "name is required"}
        old = str(kwargs.get("old", ""))
        new = str(kwargs.get("new", ""))
        rationale = str(kwargs.get("rationale", ""))
        file = str(kwargs.get("file") or "SKILL.md")
        ctx = self._ctx.get()
        session_key = ctx.session_key if ctx else None
        attribution = Attribution(actor="agent", session=session_key,
                                  agent=(ctx.metadata or {}).get("model") if ctx else None)
        # One shared decision (skills_store.gate_skill_edit) for every caller:
        # a manual skill never writes here (its owner decides); an auto skill
        # writes only when the edit's scan needs no review.
        gated = await asyncio.to_thread(gate_skill_edit, self._workspace, name, old=old,
                                        new=new, rationale=rationale, file=file)
        if "error" in gated:
            return gated
        plan = gated["plan"]
        if gated["write"]:
            result = await asyncio.to_thread(
                write_skill_edit, self._workspace, name, old=old, new=new,
                rationale=rationale, file=file, attribution=attribution)
        else:
            # gate_skill_edit leaves ``scan`` None for a manual skill (its
            # owner decides regardless of the scan, so the gate never needs
            # one to choose write vs. request) — but the request shown to a
            # person still wants the verdict/findings, so compute it here.
            scan = gated["scan"] or await asyncio.to_thread(
                scan_skill_write, plan["skill_dir"], {file: plan["after"]})
            prepared = kinds.prepare_skill_edit(
                self._workspace, name, old=old, new=new, rationale=rationale, file=file,
                attribution=attribution, plan=plan, scan=scan)
            outcome = await approval.request(
                self._workspace, prepared, session_key=session_key,
                deps=ExecDeps(attribution=attribution),
                judge=kinds.edit_judge(self._workspace, name, file=file, content=plan["after"],
                                       settings=self._judge),
                ask=self._chat.asker(ctx))
            result = approval.outcome_to_tool_result(outcome)
            if outcome.status != "applied":
                return result
        # A direct in-loop edit of an `auto` skill is itself a structural
        # improvement signal — feed the curation queue so the daily pass can
        # validate/generalize it, without relying on the agent also calling
        # skill_observe. Manual skills are the user's and curation never
        # reviews them, so only applied auto edits are logged.
        if plan["mode"] == "auto" and (result.get("ok") or result.get("status") == "applied"):
            self._log_edit_observation(name, rationale)
        return result

    def _log_edit_observation(self, name: str, rationale: str) -> None:
        from durin.agent.skill_observations import log_observation
        ctx = self._ctx.get()
        try:
            log_observation(
                self._workspace,
                skill=name,
                kind="improvement",
                issue=f"Skill '{name}' was edited in-loop during a task.",
                improvement=rationale.strip() or "(edited in-loop; no rationale given)",
                session=ctx.session_key if ctx else None,
            )
        except Exception:  # noqa: BLE001 — observation logging must never break the edit
            logger.exception("skill_edit: failed to log improvement observation for %s", name)
