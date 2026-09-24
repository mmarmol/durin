"""skill_install_deps tool — install a skill's DECLARED dependencies.

Each command runs through durin's single exec gate (ExecTool) — the same
allow/deny patterns, sandbox and logging as any shell command — never a
side-channel subprocess. Only specs the security scanner rated safe are
runnable; the 'download' kind and sudo are excluded, and privileged commands
are flagged ``needs_privileges`` for the user.

``skills.install_policy`` decides who authorizes the run: ``never`` reports the
commands only; ``auto`` runs them (the operator granted that in config);
``approve`` (the default) asks the person in this chat to approve the exact
commands, or files the request in Pending when nobody can answer. The model
cannot authorize it, and the skills judge never does."""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable

from durin.agent import approval
from durin.agent import approval_kinds_skills as kinds
from durin.agent.approval_executors import ExecDeps
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the skill whose declared dependencies to install."),
    required=["name"],
    description=(
        "Install a skill's declared dependencies (its `install` specs). The exact "
        "commands are shown to the user for approval by the tool itself; if the "
        "result is pending or rejected, continue without them and do not install "
        "them another way. Commands run through durin's exec gate; only safe "
        "package-manager specs are runnable; never escalates privileges "
        "(privileged ones are flagged for you)."
    ),
)


def _skill_dir(workspace: Path, name: str) -> Path:
    return Path(workspace) / "skills" / name


@tool_parameters(_PARAMETERS)
class SkillInstallDepsTool(Tool, ContextAware):
    """Install a skill's declared deps via ExecTool, once authorized."""

    def __init__(self, workspace, exec_run: Callable[..., Awaitable[str]] | None,
                 policy: str = "approve", chat: kinds.ChatHandles | None = None) -> None:
        self._workspace = Path(workspace)
        self._exec_run = exec_run        # async (command=...) -> output str
        self._policy = policy
        self._chat = chat or kinds.ChatHandles()
        self._ctx: ContextVar[RequestContext | None] = ContextVar(
            "skill_install_deps_ctx", default=None)

    def set_context(self, ctx: RequestContext) -> None:
        self._ctx.set(ctx)

    @property
    def name(self) -> str:
        return "skill_install_deps"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "SkillInstallDepsTool":
        from durin.agent.tools.shell import ExecTool
        exec_tool = ExecTool.create(ctx)
        policy = "approve"
        try:
            policy = ctx.app_config.skills.install_policy
        except Exception:  # noqa: BLE001
            try:
                from durin.config.loader import load_config
                policy = load_config().skills.install_policy
            except Exception:  # noqa: BLE001
                pass
        # The runner that never asks: a command the exec policy refuses fails
        # its step instead of opening a second approval inside this one.
        return cls(workspace=ctx.workspace, exec_run=exec_tool._run, policy=policy,
                   chat=kinds.ChatHandles.from_tool_context(ctx))

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skills_import import run_install_specs, runnable_install_specs
        from durin.agent.skills_store import _safe_name

        name = str(kwargs.get("name", "")).strip()
        if not _safe_name(name):
            return {"error": "invalid skill name"}
        specs = runnable_install_specs(_skill_dir(self._workspace, name))
        commands = [s["command"] for s in specs]
        privileged = [s["command"] for s in specs if s.get("needs_privileges")]
        if not commands:
            return {"would_run": [], "ran": False,
                    "note": "no safe, runnable install specs declared"}

        base = {"would_run": commands, "needs_privileges": privileged}
        if self._policy == "never":
            return {**base, "ran": False,
                    "note": "install_policy=never — reporting only, not running"}
        if self._policy == "auto":
            # The operator granted dependency installs in config, out of band.
            results = await run_install_specs(specs, exec_run=self._exec_run)
            return {**base, "ran": True, "results": results}
        ctx = self._ctx.get()
        outcome = await approval.request(
            self._workspace, kinds.prepare_skill_deps(self._workspace, name, specs),
            session_key=ctx.session_key if ctx else None,
            deps=ExecDeps(exec_run=self._exec_run), ask=self._chat.asker(ctx))
        out = {**base, **approval.outcome_to_tool_result(outcome),
               "ran": outcome.status == "applied"}
        if outcome.status == "applied":
            out["results"] = (outcome.result or {}).get("results", [])
        return out
