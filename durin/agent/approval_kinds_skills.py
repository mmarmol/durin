"""Approval kinds for skills: installing a quarantined skill, editing a skill,
and installing a skill's declared dependencies.

Each kind has three parts:

* ``prepare_*`` builds the request a gated tool files: a summary, the detail a
  person reviews (verdict, findings, source, diff or commands) and the payload
  the executor needs.
* a hash of the state the request would change, taken when the request is
  filed and again right before it runs, so an approval never applies to content
  nobody reviewed: a re-fetched quarantine, a skill file that changed, or
  declared dependencies that are different now.
* an executor that performs the payload server-side and records who approved it.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
from pathlib import Path

from durin.agent import skills_import as si
from durin.agent import skills_store as ss
from durin.agent.approval_executors import ApprovalExecError, ExecDeps, Prepared, register
from durin.agent.skills_frontmatter import frontmatter_broken, split_frontmatter

# Session key recorded on requests filed by autonomous skill maintenance
# (curation) when the caller carries no session of its own. The ``system:``
# prefix classifies it as autonomous: nobody is asked in-turn.
AUTONOMOUS_SKILLS_SESSION = "system:skills"

_DIFF_CAP = 6000
_SEVERITY_RANK = {"dangerous": 0, "high": 1, "caution": 2, "info": 3}
# SKILL.md keys durin restamps without changing what a skill says or runs:
# curation's review cursor and the provenance record (review stamps included).
_BOOKKEEPING_KEYS = ("provenance", "curation_rules")


def _sha(*parts: bytes) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part)
        h.update(b"\0")
    return h.hexdigest()


def _brief(findings: list[dict], limit: int = 3) -> str:
    """The strongest reasons on one line, for summaries every surface shows."""
    top = sorted(findings, key=lambda f: _SEVERITY_RANK.get(str(f.get("severity")), 4))
    return "; ".join(f"{f.get('detail')} ({f.get('where')})" for f in top[:limit])


def _attribution(payload: dict) -> "ss.Attribution":
    return ss.Attribution(actor=str(payload.get("actor") or "agent"),
                          session=payload.get("session"), agent=payload.get("agent"))


def _approver(deps: ExecDeps) -> tuple[str | None, str | None]:
    """(approval id, decider kind) of the request being executed."""
    meta = deps.extra.get("approval") or {}
    return meta.get("id"), (meta.get("decided_by") or {}).get("kind")


def _diff(before: str, after: str, fromfile: str, tofile: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=fromfile, tofile=tofile))[:_DIFF_CAP]


# --- skill_install ---------------------------------------------------------------

def quarantine_dir(workspace: Path, name: str) -> Path | None:
    """The quarantine directory for ``name``; None for a name that could escape it."""
    if not si._safe_qname(name):
        return None
    return Path(workspace) / ".durin" / "import-quarantine" / name


def quarantine_source(qdir: Path, default: str) -> str:
    """The source ref recorded at fetch time in ``.scan.json``."""
    sj = Path(qdir) / ".scan.json"
    if sj.is_file():
        try:
            return str(json.loads(sj.read_text()).get("source") or default)
        except Exception:  # noqa: BLE001 — an unreadable cache falls back to the name
            pass
    return default


def install_hash(workspace: Path, payload: dict) -> str:
    """The quarantined tree's content hash (``.scan.json`` excluded, so a
    re-audit of the same bytes keeps the approval), bound to the replace flag
    the person saw."""
    qdir = quarantine_dir(workspace, str(payload.get("quarantine") or ""))
    if qdir is None or not (qdir / "SKILL.md").is_file():
        return "absent"
    return f"{si._content_hash(qdir)}:replace={bool(payload.get('replace'))}"


def _install_reason(gate: dict) -> str:
    parts: list[str] = []
    if gate["findings"]:
        parts.append(f"{gate['verdict']}: {_brief(gate['findings'])}")
    if gate["carries_code"]:
        parts.append("carries code: " + ", ".join(gate["code_artifacts"][:3]))
    return "; ".join(parts) or "its source is not on the trusted list"


def prepare_skill_install(workspace: Path, qdir: Path, *, gate: dict, source: str,
                          replace: bool, attribution: "ss.Attribution") -> Prepared:
    """The request for installing the quarantined skill ``qdir``; ``gate`` is
    ``skills_import.install_gate`` for that directory."""
    name = gate["name"]
    new_md = (Path(qdir) / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    current = Path(workspace) / "skills" / name / "SKILL.md"
    old_md = current.read_text(encoding="utf-8", errors="replace") if current.is_file() else ""
    payload = {"quarantine": Path(qdir).name, "source": source, "replace": bool(replace),
               "verdict": gate["verdict"], "actor": attribution.actor,
               "session": attribution.session, "agent": attribution.agent}
    return Prepared(
        kind="skill_install",
        summary=f"install skill {name!r} from {source} ({_install_reason(gate)})",
        detail={"skill": name, "source": source, "verdict": gate["verdict"],
                "needs": gate["action"], "findings": gate["findings"],
                "carries_code": gate["carries_code"],
                "code_artifacts": gate["code_artifacts"], "replace": bool(replace),
                "diff": _diff(old_md, new_md,
                              f"a/{name}/SKILL.md" if old_md else "/dev/null",
                              f"b/{name}/SKILL.md")},
        payload=payload,
        change_hash=install_hash(workspace, payload),
    )


async def execute_install(workspace: Path, payload: dict, deps: ExecDeps) -> dict:
    """Install the approved quarantine. ``confirmed`` is granted because someone
    decided; ``override`` only when the verdict they saw was dangerous. If the
    verdict rose since (a re-audit of the quarantine), the install refuses and
    the request fails instead of installing what nobody approved."""
    name = str(payload.get("quarantine") or "")
    qdir = quarantine_dir(workspace, name)
    if qdir is None or not (qdir / "SKILL.md").is_file():
        raise ApprovalExecError(f"not in quarantine: {name}")
    approval_id, approved_by = _approver(deps)
    try:
        return await asyncio.to_thread(
            si.install_imported_skill, Path(workspace), qdir,
            source=str(payload.get("source") or name),
            allowlist=ss._import_allowlist(), confirmed=True,
            override=payload.get("verdict") == "dangerous",
            replace=bool(payload.get("replace")),
            attribution=_attribution(payload),
            approval_id=approval_id, approved_by=approved_by)
    except si.SkillImportRefused as exc:
        raise ApprovalExecError(str(exc)) from exc


# --- skill_edit ------------------------------------------------------------------

def _reviewed_bytes(target: Path, file: str) -> bytes:
    """The target file as a reviewer judged it. For SKILL.md, durin's own
    bookkeeping under ``metadata.durin`` is left out: curation restamps it the
    same night it files an edit, and that must not make the edit stale."""
    raw = target.read_bytes()
    if file != "SKILL.md":
        return raw
    text = raw.decode("utf-8", errors="replace")
    if frontmatter_broken(text):
        return raw
    data, body = split_frontmatter(text)
    meta = data.get("metadata")
    durin = meta.get("durin") if isinstance(meta, dict) else None
    if isinstance(durin, dict):
        for key in _BOOKKEEPING_KEYS:
            durin.pop(key, None)
    return (json.dumps(data, sort_keys=True, default=str) + "\n" + body).encode("utf-8")


def edit_hash(workspace: Path, payload: dict) -> str:
    """sha256 of the target file as it is now (empty when absent) plus the
    payload: approving applies exactly this replace to exactly this content."""
    name = str(payload.get("name") or "")
    file = str(payload.get("file") or "SKILL.md")
    root = ss._resolve_skill_dir(Path(workspace), name)
    if root is None:
        content = b"\0skill-absent"
    else:
        target = ss._safe_target(root, file)
        content = (_reviewed_bytes(target, file)
                   if target is not None and target.is_file() else b"")
    return _sha(b"skill_edit", content, json.dumps(payload, sort_keys=True).encode("utf-8"))


def prepare_skill_edit(workspace: Path, name: str, *, old: str, new: str, rationale: str,
                       file: str, attribution: "ss.Attribution", plan: dict,
                       scan: "ss.WriteScan") -> Prepared:
    """The request for one bounded edit; ``plan`` is ``plan_skill_edit`` and
    ``scan`` its ``scan_skill_write``."""
    reasons: list[str] = []
    if plan["mode"] == "manual":
        reasons.append("the user owns this skill")
    if scan.needs_review:
        reasons.append(f"its security scan becomes {scan.after}: "
                       f"{_brief(scan.new_findings or scan.findings)}")
    payload = {"name": name, "file": file, "old": old, "new": new, "rationale": rationale,
               "actor": attribution.actor, "session": attribution.session,
               "agent": attribution.agent}
    return Prepared(
        kind="skill_edit",
        summary=f"edit {file} of skill {name!r} ({'; '.join(reasons)})",
        detail={"skill": name, "file": file, "mode": plan["mode"], "rationale": rationale,
                "verdict": scan.after, "verdict_before": scan.before,
                "findings": scan.findings, "new_findings": scan.new_findings,
                "diff": _diff(plan["before"], plan["after"],
                              f"a/{name}/{file}", f"b/{name}/{file}")},
        payload=payload,
        change_hash=edit_hash(workspace, payload),
    )


async def execute_edit(workspace: Path, payload: dict, deps: ExecDeps) -> dict:
    """Land the approved edit through the write an allowed edit uses."""
    approval_id, approved_by = _approver(deps)
    res = await asyncio.to_thread(
        ss.write_skill_edit, Path(workspace), str(payload.get("name") or ""),
        old=str(payload.get("old") or ""), new=str(payload.get("new") or ""),
        rationale=str(payload.get("rationale") or ""),
        file=str(payload.get("file") or "SKILL.md"),
        attribution=_attribution(payload),
        approval_id=approval_id, approved_by=approved_by)
    if not res.get("ok"):
        raise ApprovalExecError(str(res.get("error") or "the edit could not be applied"))
    return res


# --- skill_deps ------------------------------------------------------------------

def deps_hash(workspace: Path, payload: dict) -> str:
    """sha256 over the skill's runnable install specs as declared now: an
    approval runs the commands that were shown, and only while the skill still
    declares them."""
    name = str(payload.get("name") or "")
    specs = (si.runnable_install_specs(Path(workspace) / "skills" / name)
             if ss._safe_name(name) else [])
    return _sha(b"skill_deps",
                json.dumps({"name": name, "specs": specs}, sort_keys=True).encode("utf-8"))


def prepare_skill_deps(workspace: Path, name: str, specs: list[dict]) -> Prepared:
    """The request for running ``specs`` (from ``runnable_install_specs``)."""
    commands = [s["command"] for s in specs]
    privileged = [s["command"] for s in specs if s.get("needs_privileges")]
    payload = {"name": name, "specs": specs}
    summary = f"install the declared dependencies of skill {name!r}: {'; '.join(commands)}"
    return Prepared(
        kind="skill_deps",
        summary=summary[:300],
        detail={"skill": name, "packages": "; ".join(commands), "commands": commands,
                "needs_privileges": privileged},
        payload=payload,
        change_hash=deps_hash(workspace, payload),
    )


async def execute_deps(workspace: Path, payload: dict, deps: ExecDeps) -> dict:
    """Run the approved commands through the exec runner the caller provided
    (the gateway's ExecTool, with its own guards)."""
    if deps.exec_run is None:
        raise ApprovalExecError(
            "installing dependencies needs the running gateway's exec tool; "
            "approve it from the chat or the web UI")
    specs = [s for s in (payload.get("specs") or [])
             if isinstance(s, dict) and s.get("command")]
    results = await si.run_install_specs(specs, exec_run=deps.exec_run)
    approval_id, approved_by = _approver(deps)
    si._audit(Path(workspace), name=payload.get("name"), action="install_deps",
              commands=[s["command"] for s in specs],
              succeeded=[r["command"] for r in results if r.get("success")],
              approval_id=approval_id, approved_by=approved_by)
    return {"ran": True, "results": results}


def register_all() -> None:
    """Register the three skill kinds with the approval executor registry."""
    register("skill_install", hash_fn=install_hash, execute_fn=execute_install)
    register("skill_edit", hash_fn=edit_hash, execute_fn=execute_edit)
    register("skill_deps", hash_fn=deps_hash, execute_fn=execute_deps)


register_all()
