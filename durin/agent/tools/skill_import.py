"""skill_import tool — import a skill from any source through the security floor.

Auto-discovered into the agent's ``core`` toolset (like ``skill_audit``).
Source-agnostic: a local path, a direct ``https://…/SKILL.md``, or
``github:owner/repo[/subdir]``. Actions:

- ``resolve``  — list the skill candidates a source points at (a repo may hold
  many; the agent disambiguates).
- ``fetch``    — download ONE candidate into ``.durin/import-quarantine/`` and
  run the security scan. If the source resolves to many, returns the candidate
  list to pick from instead.
- ``install``  — install a quarantined skill through the import gate. Nothing
  the model passes can authorize it: a skill the gate allows installs at once;
  a flagged one installs when ``skills.install_policy`` is ``auto`` (never for a
  dangerous verdict), when the configured skills judge clears it, or when a
  person approves it — asked in this chat, or later from ``durin approvals``.
- ``reject``   — discard a quarantined skill.
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
    replace=BooleanSchema(
        description=("install: overwrite an existing skill of the same name. Without "
                     "this, install refuses when the name already exists."),
        default=False,
    ),
    description=(
        "Import a skill from any source (local path, URL, github:owner/repo) "
        "through the security floor: resolve -> fetch (quarantine+scan) -> "
        "install / reject. When the gate flags a skill, install asks the user to "
        "approve it by itself — do not ask for that approval yourself. If the "
        "result is pending or rejected, continue without the skill and do not "
        "reach the same effect another way."
    ),
)


@tool_parameters(_PARAMETERS)
class SkillImportTool(Tool, ContextAware):
    """skill_import tool — import through the security floor."""

    # Core-only: acquiring new skills stays a primary-agent decision.
    _scopes = {"core"}

    def __init__(self, workspace: str | Path, allowlist: list[str] | None = None,
                 caps: tuple[int, int, int] | None = None,
                 judge: tuple[str, str, str] | None = None,
                 install_policy: str = "approve",
                 exec_run: Any = None,
                 chat: kinds.ChatHandles | None = None) -> None:
        self._workspace = Path(workspace).expanduser()
        self._allowlist = list(allowlist or [])
        self._caps = caps or (100, 3 * 1024 * 1024, 1024 * 1024)
        self._judge = judge or ("off", "", "caution")  # trigger off unless config says otherwise
        self._install_policy = install_policy
        self._exec_run = exec_run
        self._chat = chat or kinds.ChatHandles()
        self._ctx: ContextVar[RequestContext | None] = ContextVar("skill_import_ctx", default=None)

    def set_context(self, ctx: RequestContext) -> None:
        self._ctx.set(ctx)

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
        install_policy = "approve"
        try:
            install_policy = str(ctx.app_config.skills.install_policy)
        except Exception:  # noqa: BLE001
            pass
        exec_run = None
        try:
            from durin.agent.tools.shell import ExecTool
            # The runner that never asks: a dependency command the exec policy
            # refuses fails its step instead of opening a second approval
            # inside the install that is being carried out.
            exec_run = ExecTool.create(ctx)._run
        except Exception:  # noqa: BLE001
            pass
        return cls(workspace=ctx.workspace, allowlist=allowlist, caps=caps, judge=judge,
                   install_policy=install_policy, exec_run=exec_run,
                   chat=kinds.ChatHandles.from_tool_context(ctx))

    @property
    def _qroot(self) -> Path:
        return self._workspace / ".durin" / "import-quarantine"

    @staticmethod
    def _cand_dict(c: Any) -> dict:
        return {"name": c.name, "ref": c.ref, "kind": c.kind, "detail": c.detail}

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skill_resolve import resolve_candidates
        from durin.agent.skills_import import (
            decide_action,
            fetch_candidate,
            reject_quarantined,
            validate_skill,
        )
        from durin.security.skill_scan import scan_skill

        action = (str(kwargs.get("action") or "resolve")).strip()
        source = str(kwargs.get("source", "")).strip()
        name = str(kwargs.get("name", "")).strip()
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
            return await self._install(name, replace)

        if action == "reject":
            if not name:
                return {"error": "name is required for reject"}
            return await asyncio.to_thread(reject_quarantined, self._workspace, name)

        return {"error": f"unknown action: {action!r}"}

    async def _install(self, name: str, replace: bool) -> Any:
        from durin.agent.skills_import import (
            SkillImportRefused,
            _safe_qname,
            install_gate,
            install_imported_skill,
        )
        from durin.agent.skills_store import Attribution

        if not name:
            return {"error": "name is required for install"}
        if not _safe_qname(name):
            return {"error": "invalid name"}
        qdir = self._qroot / name
        if not (qdir / "SKILL.md").is_file():
            return {"error": f"not in quarantine: {name}"}
        src = kinds.quarantine_source(qdir, default=name)
        gate = await asyncio.to_thread(install_gate, qdir, source=src, allowlist=self._allowlist)
        if not gate["valid"]:
            return {"refused": "invalid", "verdict": "",
                    "message": f"invalid skill: {gate['errors']}"}
        if not replace and (self._workspace / "skills" / gate["name"]).exists():
            # Refuse before anyone is asked: an approval would only fail later.
            return {"refused": "exists", "verdict": gate["verdict"],
                    "message": f"skill already exists: {gate['name']}; "
                               "pass replace=true to overwrite it"}
        ctx = self._ctx.get()
        session_key = ctx.session_key if ctx else None
        attribution = Attribution(actor="import", session=session_key,
                                  agent=(ctx.metadata or {}).get("model") if ctx else None)
        action = gate["action"]
        # Overwriting an existing skill is an ownership decision, not a safety
        # one: even a scan-safe/trusted or judge-clearable install must not
        # replace something already there without a person saying so. Both
        # shortcuts below (the direct "allow" install and the "auto" policy
        # pre-authorization) apply only to a fresh name; replacing routes
        # through approval.request with no judge, same as any other request.
        replacing = replace and (self._workspace / "skills" / gate["name"]).exists()
        if not replacing and (action == "allow"
                              or (action == "confirm" and self._install_policy == "auto")):
            # Nothing to decide (safe, trusted, no code), or the operator granted
            # flagged installs ahead of time in config. A dangerous verdict is
            # never covered by policy: only a person can accept that one.
            try:
                result = await asyncio.to_thread(
                    install_imported_skill, self._workspace, qdir, source=src,
                    allowlist=self._allowlist, confirmed=(action == "confirm"), replace=replace,
                    attribution=attribution,
                    approved_by=None if action == "allow" else "policy")
            except SkillImportRefused as exc:
                return {"refused": exc.action, "verdict": exc.verdict, "message": str(exc)}
        else:
            prepared = kinds.prepare_skill_install(
                self._workspace, qdir, gate=gate, source=src, replace=replace,
                attribution=attribution)
            judge = None if replacing else kinds.install_judge(
                qdir, action=action, findings=gate["findings"], settings=self._judge)
            outcome = await approval.request(
                self._workspace, prepared, session_key=session_key,
                deps=ExecDeps(exec_run=self._exec_run, attribution=attribution),
                judge=judge, ask=self._chat.asker(ctx), origin=approval.request_origin(ctx))
            result = approval.outcome_to_tool_result(outcome)
            if outcome.status != "applied":
                return result
        await self._auto_install_deps(gate["name"], result)
        return result

    async def _auto_install_deps(self, skill_name: str, result: dict) -> None:
        """With ``install_policy: auto`` the operator pre-authorized dependency
        runs: install the new skill's declared specs through the exec runner."""
        if self._install_policy != "auto" or self._exec_run is None:
            return
        skill_dir = self._workspace / ".durin" / "skills" / skill_name
        if not skill_dir.is_dir():
            skill_dir = self._workspace / "skills" / skill_name
        if not skill_dir.is_dir():
            return
        try:
            from durin.agent.skills_import import run_install_specs, runnable_install_specs
            specs = runnable_install_specs(skill_dir)
            if specs:
                deps_results = await run_install_specs(specs, exec_run=self._exec_run)
                if deps_results:
                    result["deps_installed"] = deps_results
        except Exception:  # noqa: BLE001 — a dependency failure never undoes the install
            pass
