"""skill_acquire_seed tool — §6.C. Given ONE registry ref the dream chose from a raw
skill_search hit, return a RISK-FREE seed (gate verdict 'allow') to author from, or
{seed: null} to tell the dream to pick another. The gate runs in code
(acquire_safe_seed), so a risky/un-allowlisted ref is never handed back. Lives in the
dream phase-2 toolset; the in-session agent uses raw skill_search/skill_import/
ask_user_question instead (a human is present to approve risky candidates)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    source=StringSchema(
        "A registry ref from a skill_search hit to evaluate as a seed "
        "(e.g. 'github:owner/repo/skill' or 'clawhub:slug')."),
    required=["source"],
    description=(
        "Given a ref from a skill_search result, return a RISK-FREE seed (SKILL.md "
        "body) to author a new skill from — only if it clears the security gate. "
        "Returns {seed: null} when it needs user consent or can't be fetched; then "
        "pick another hit or author from scratch. Never installs; never returns "
        "risky code."
    ),
)


@tool_parameters(_PARAMETERS)
class SkillAcquireSeedTool(Tool):
    """Return a safe registry seed for one chosen ref, or null (pick another)."""

    # Dream-only: Path A (in-session) uses the raw skill_search/skill_import/
    # ask_user_question tools so the user approves risky candidates. This gated tool
    # silently skips risky ones, which is correct ONLY for the autonomous dream.
    _scopes = {"dream"}

    def __init__(self, workspace: str | Path, allowlist: list[str]) -> None:
        self._workspace = Path(workspace)
        self._allowlist = list(allowlist)

    @property
    def name(self) -> str:
        return "skill_acquire_seed"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "SkillAcquireSeedTool":
        allowlist: list[str] = []
        try:
            sk = ctx.app_config.skills
        except Exception:  # noqa: BLE001
            try:
                from durin.config.loader import load_config
                sk = load_config().skills
            except Exception:  # noqa: BLE001
                sk = None
        if sk is not None:
            allowlist = list(sk.security.allowlist)
        return cls(workspace=ctx.workspace, allowlist=allowlist)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skill_acquire import acquire_safe_seed

        source = str(kwargs.get("source", "")).strip()
        if not source:
            return {"seed": None, "note": "source is required"}
        seed = await acquire_safe_seed(
            self._workspace, source, allowlist=self._allowlist)
        if seed is None:
            return {"seed": None,
                    "note": "needs user consent or unfetchable — pick another hit "
                            "or author from scratch"}
        return {"seed": seed,
                "note": "adapt this seed; it passed the security gate"}
