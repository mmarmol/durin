"""skill_import tool — import a skill from any source through the §8.C floor.

Auto-discovered into the agent's ``core`` toolset (like ``skill_audit``).
Source-agnostic: a local path, a direct ``https://…/SKILL.md``, or
``github:owner/repo[/subdir]``. Actions:

- ``resolve``  — list the skill candidates a source points at (a repo may hold
  many; the agent disambiguates).
- ``fetch``    — download ONE candidate into ``.durin/import-quarantine/`` and
  run the §8.C scan. If the source resolves to many, returns the candidate list
  to pick from instead.
- ``install``  — install a quarantined skill THROUGH THE GATE: ``confirm`` is
  required when it carries code / is caution / is out-of-allowlist; ``override``
  is required when the verdict is dangerous. The refusal is enforced in
  :func:`install_imported_skill`, not here.
- ``reject``   — discard a quarantined skill.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    action=StringSchema(
        "One of: 'resolve' (list candidates for a source), 'fetch' (download one "
        "candidate to quarantine + scan), 'install' (install a quarantined skill "
        "through the gate), 'reject' (discard a quarantined skill). Default 'resolve'."
    ),
    source=StringSchema(
        "Import source for resolve/fetch: a local path, a direct https URL to a "
        "SKILL.md, or 'github:owner/repo[/subdir]'. To fetch one of several "
        "candidates, pass that candidate's 'ref' as the source."
    ),
    name=StringSchema(
        "Quarantined skill name for install/reject (the 'quarantined' value a "
        "prior fetch returned)."
    ),
    confirm=BooleanSchema(
        description=("install: confirm a skill that carries code / is caution / is "
                     "out-of-allowlist. Does NOT bypass a dangerous verdict."),
        default=False,
    ),
    override=BooleanSchema(
        description=("install: explicitly override a DANGEROUS verdict. Only set this "
                     "when the user has explicitly told you to force the install."),
        default=False,
    ),
    replace=BooleanSchema(
        description=("install: overwrite an existing skill of the same name. Without "
                     "this, install refuses when the name already exists."),
        default=False,
    ),
    description=(
        "Import a skill from any source (local path, URL, github:owner/repo) "
        "through the §8.C security floor: resolve -> fetch (quarantine+scan) -> "
        "install (gated by verdict) / reject."
    ),
)


@tool_parameters(_PARAMETERS)
class SkillImportTool(Tool, ContextAware):
    """skill_import tool — §6.B import over the §8.C floor."""

    def __init__(self, workspace: str | Path, allowlist: list[str] | None = None,
                 caps: tuple[int, int, int] | None = None,
                 judge: tuple[str, str, str] | None = None) -> None:
        self._workspace = Path(workspace).expanduser()
        self._allowlist = list(allowlist or [])
        self._caps = caps or (100, 3 * 1024 * 1024, 1024 * 1024)
        self._judge = judge or ("off", "", "caution")  # trigger off unless config says otherwise
        self._session: ContextVar[str | None] = ContextVar("skill_import_session", default=None)
        self._model: ContextVar[str | None] = ContextVar("skill_import_model", default=None)

    def set_context(self, ctx: RequestContext) -> None:
        self._session.set(ctx.session_key)
        self._model.set((ctx.metadata or {}).get("model"))

    @property
    def name(self) -> str:
        return "skill_import"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @property
    def read_only(self) -> bool:
        return False

    @classmethod
    def create(cls, ctx: Any) -> "SkillImportTool":
        si = None
        try:
            si = ctx.app_config.skills.security
        except Exception:  # noqa: BLE001 — config shape varies; fall back to loader
            try:
                from durin.config.loader import load_config
                si = load_config().skills.security
            except Exception:  # noqa: BLE001
                si = None
        allowlist = list(si.allowlist) if si is not None else []
        caps = (si.max_files, si.max_total_bytes, si.max_file_bytes) if si is not None else None
        judge = None
        if si is not None:
            j = si.llm_judge
            judge = (str(j.trigger or "off"), str(j.model or ""), str(j.max_severity or "caution"))
        return cls(workspace=ctx.workspace, allowlist=allowlist, caps=caps, judge=judge)

    @property
    def _qroot(self) -> Path:
        return self._workspace / ".durin" / "import-quarantine"

    @staticmethod
    def _cand_dict(c: Any) -> dict:
        return {"name": c.name, "ref": c.ref, "kind": c.kind, "detail": c.detail}

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skill_resolve import resolve_candidates
        from durin.agent.skills_import import (
            SkillImportRefused,
            decide_action,
            fetch_candidate,
            install_imported_skill,
            reject_quarantined,
            validate_skill,
        )
        from durin.security.skill_scan import scan_skill

        action = (str(kwargs.get("action") or "resolve")).strip()
        source = str(kwargs.get("source", "")).strip()
        name = str(kwargs.get("name", "")).strip()
        confirm = bool(kwargs.get("confirm", False))
        override = bool(kwargs.get("override", False))
        replace = bool(kwargs.get("replace", False))

        if action == "resolve":
            if not source:
                return {"error": "source is required for resolve"}
            res = await asyncio.to_thread(resolve_candidates, source)
            return {"candidates": [self._cand_dict(c) for c in res.candidates],
                    "unresolved_reason": res.unresolved_reason}

        if action == "fetch":
            if not source:
                return {"error": "source is required for fetch"}
            res = await asyncio.to_thread(resolve_candidates, source)
            if not res.candidates:
                return {"unresolved_reason": res.unresolved_reason
                        or "no skill found at source"}
            if len(res.candidates) > 1:
                return {"candidates": [self._cand_dict(c) for c in res.candidates],
                        "note": "multiple skills found; fetch one by passing its 'ref' as source"}
            cand = res.candidates[0]
            mf, mt, mfb = self._caps
            jt, jm, jms = self._judge
            qdir = await asyncio.to_thread(
                fetch_candidate, cand, quarantine_root=self._qroot,
                max_files=mf, max_total_bytes=mt, max_file_bytes=mfb,
                judge_trigger=jt, judge_model=jm, judge_max_severity=jms,
                allowlist=self._allowlist)
            rep = scan_skill(qdir)
            vr = validate_skill(qdir)
            needs = decide_action(cand.ref, verdict=rep.verdict,
                                  carries_code=vr.carries_code, allowlist=self._allowlist)
            return {
                "quarantined": cand.name,
                "source": cand.ref,
                "verdict": rep.verdict,
                "carries_code": vr.carries_code,
                "needs": needs,
                "findings": [{"category": f.category, "severity": f.severity,
                              "where": f.where, "detail": f.detail} for f in rep.findings],
            }

        if action == "install":
            if not name:
                return {"error": "name is required for install"}
            qdir = self._qroot / name
            if not (qdir / "SKILL.md").is_file():
                return {"error": f"not in quarantine: {name}"}
            src = name
            sj = qdir / ".scan.json"
            if sj.is_file():
                try:
                    src = json.loads(sj.read_text()).get("source", name)
                except Exception:  # noqa: BLE001
                    pass
            try:
                from durin.agent.skills_store import Attribution
                attribution = Attribution(actor="import", session=self._session.get(),
                                         agent=self._model.get())
                return await asyncio.to_thread(
                    install_imported_skill, self._workspace, qdir,
                    source=src, allowlist=self._allowlist,
                    confirmed=confirm, override=override, replace=replace,
                    attribution=attribution)
            except SkillImportRefused as exc:
                return {"refused": exc.action, "verdict": exc.verdict, "message": str(exc)}

        if action == "reject":
            if not name:
                return {"error": "name is required for reject"}
            return await asyncio.to_thread(reject_quarantined, self._workspace, name)

        return {"error": f"unknown action: {action!r}"}
