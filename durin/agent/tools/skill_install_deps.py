"""skill_install_deps tool — P6 #1. Install a skill's DECLARED dependencies, only
after explicit user approval. The default call is a DRY RUN that lists the exact
commands; ``confirm=true`` runs them. Each command is executed through durin's single
exec gate (ExecTool) — same allow/deny patterns, sandbox, and logging as any shell
command — not a side-channel subprocess. Policy `skills.install_policy`: 'never' =
report only; 'approve' = dry-run→confirm (default); 'auto' = run without confirm.
Only specs the §8.C scanner rated safe are runnable; 'download' kind and sudo are
excluded — privileged commands are flagged `needs_privileges` for the user."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the skill whose declared dependencies to install."),
    confirm=BooleanSchema(
        description=(
            "false (default) = DRY RUN: only report the commands. true = run them — pass "
            "true ONLY after the user explicitly approved the listed commands."
        ),
        default=False,
    ),
    required=["name"],
    description=(
        "Install a skill's declared dependencies (its `install` specs) after user "
        "approval. Default is a dry run listing the exact commands; call again with "
        "confirm=true to run them once the user approved. Commands run through "
        "durin's exec gate; only safe package-manager specs are runnable; never "
        "escalates privileges (privileged ones are flagged for you)."
    ),
)


def _skill_dir(workspace: Path, name: str) -> Path:
    return Path(workspace) / "skills" / name


@tool_parameters(_PARAMETERS)
class SkillInstallDepsTool(Tool):
    """Install a skill's declared deps via ExecTool; dry-run by default."""

    def __init__(self, workspace, exec_run: Callable[..., Awaitable[str]] | None,
                 policy: str = "approve") -> None:
        self._workspace = Path(workspace)
        self._exec_run = exec_run        # async (command=...) -> output str
        self._policy = policy

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
        return cls(workspace=ctx.workspace, exec_run=exec_tool.execute, policy=policy)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skills_import import runnable_install_specs

        name = str(kwargs.get("name", "")).strip()
        confirm = bool(kwargs.get("confirm", False))
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
        run = self._policy == "auto" or confirm
        if not run:
            return {**base, "ran": False,
                    "note": "DRY RUN — review with the user (note any needs_privileges), "
                            "then call again with confirm=true"}
        results = []
        for cmd in commands:
            output = await self._exec_run(command=cmd)
            results.append({"command": cmd, "output": str(output)[-2000:]})
        return {**base, "ran": True, "results": results}
