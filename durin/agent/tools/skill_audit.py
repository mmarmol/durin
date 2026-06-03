"""skill_audit tool — run the §8.C security scan on an existing skill.

Auto-discovered into the agent's ``core`` toolset (like ``skill_edit`` /
``skill_write``). Resolves a skill (by ``name`` under the workspace ``skills/``
dir, or by an absolute/relative ``path``), runs the format lint
(:func:`validate_skill`) plus the deterministic security scan
(:func:`scan_skill`), and returns a verdict + findings so the agent can decide
whether to trust/import it.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    name=StringSchema(
        "Name of the skill to audit (directory under the workspace 'skills/'). "
        "Provide either 'name' or 'path'."
    ),
    path=StringSchema(
        "Path to a skill directory to audit (overrides 'name'). Use to audit a "
        "skill that is not yet in the workspace 'skills/' dir."
    ),
    deep=BooleanSchema(
        description=("Also run the LLM judge (semantic review of what regex can't see) "
                     "on top of the deterministic scan. Slower; needs a judge model."),
        default=False,
    ),
    description=(
        "Audit a skill for security: runs the format lint + deterministic "
        "security scan, returns a verdict (safe/caution/dangerous) and "
        "findings. Use before trusting/importing a skill."
    ),
)


@tool_parameters(_PARAMETERS)
class SkillAuditTool(Tool):
    """skill_audit tool — §8.C scan on an existing skill."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "skill_audit"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> "SkillAuditTool":
        return cls(workspace=ctx.workspace)

    def _resolve(self, name: str, path: str) -> tuple[Path | None, str | None]:
        """Resolve the target skill dir from name|path. Returns (dir, error)."""
        from durin.agent.skills_store import _safe_name

        if path:
            target = Path(path).expanduser()
            if not target.is_dir():
                return None, f"not a directory: {path}"
            return target, None
        if not name:
            return None, "provide either 'name' or 'path'"
        if not _safe_name(name):
            return None, f"unsafe skill name: {name!r}"
        target = self._workspace / "skills" / name
        if not target.is_dir():
            return None, f"skill not found: {name}"
        return target, None

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skills_import import validate_skill
        from durin.security.skill_scan import scan_skill

        name = str(kwargs.get("name", "")).strip()
        path = str(kwargs.get("path", "")).strip()
        target, error = self._resolve(name, path)
        if error:
            return {"error": error}
        assert target is not None  # guarded above

        rep = validate_skill(target)
        if bool(kwargs.get("deep", False)):
            from durin.config.loader import load_config
            from durin.security.skill_judge import audit_skill
            try:
                j = load_config().skills.security.llm_judge
                model, max_sev = str(j.model or ""), str(j.max_severity or "caution")
            except Exception:  # noqa: BLE001
                model, max_sev = "", "caution"
            scan = audit_skill(target, judge_enabled=True, judge_model=model,
                               judge_max_severity=max_sev)
        else:
            scan = scan_skill(target)
        return {
            "name": rep.name,
            "verdict": scan.verdict,
            "carries_code": rep.carries_code,
            "findings": [
                {
                    "category": f.category,
                    "severity": f.severity,
                    "where": f.where,
                    "detail": f.detail,
                }
                for f in scan.findings
            ],
            "warnings": rep.warnings,
            "errors": rep.errors,
        }
