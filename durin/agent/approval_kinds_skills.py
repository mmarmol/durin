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
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, ContextManager

from durin.agent import approval
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
    the person saw. When replacing, the currently installed skill's bytes are
    folded in too: a ``replace`` overwrites it, so an edit to it between the
    request and the decision must also make the approval stale, not just a
    re-fetch of the quarantine."""
    qdir = quarantine_dir(workspace, str(payload.get("quarantine") or ""))
    if qdir is None or not (qdir / "SKILL.md").is_file():
        return "absent"
    base = f"{si._content_hash(qdir)}:replace={bool(payload.get('replace'))}"
    if not payload.get("replace"):
        return base
    name = si.validate_skill(qdir).name
    target = Path(workspace) / "skills" / name / "SKILL.md"
    installed = _reviewed_bytes(target, "SKILL.md") if target.is_file() else b""
    return _sha(base.encode("utf-8"), installed)


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
    decided; ``override`` only when the verdict they saw was dangerous — and
    only when a person (not a judge or policy) is who decided it, since
    overriding a dangerous verdict is a person's call to make. If the verdict
    rose since (a re-audit of the quarantine), the install refuses and the
    request fails instead of installing what nobody approved."""
    name = str(payload.get("quarantine") or "")
    qdir = quarantine_dir(workspace, name)
    if qdir is None or not (qdir / "SKILL.md").is_file():
        raise ApprovalExecError(f"not in quarantine: {name}")
    approval_id, approved_by = _approver(deps)
    verdict = payload.get("verdict")
    if verdict == "dangerous" and approved_by not in ("user", "operator"):
        raise ApprovalExecError("a dangerous install needs a person's approval")
    try:
        return await asyncio.to_thread(
            si.install_imported_skill, Path(workspace), qdir,
            source=str(payload.get("source") or name),
            allowlist=ss._import_allowlist(), confirmed=True,
            override=verdict == "dangerous",
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
    try:
        rendered = json.dumps(data, sort_keys=True, default=str)
    except TypeError:
        # A YAML mapping with mixed-type keys (e.g. an int and a str key in
        # the same table) cannot be sorted for a stable rendering. Falling
        # back to the raw bytes still binds the hash to exactly what was
        # reviewed; it just skips stripping the bookkeeping keys in this rare
        # case, which only makes the approval go stale a bit more eagerly.
        return raw
    return (rendered + "\n" + body).encode("utf-8")


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
    """Land the approved edit through the write an allowed edit uses. A
    manual skill is its owner's; only a person (not a judge or policy) may
    consent to changing it."""
    approval_id, approved_by = _approver(deps)
    name = str(payload.get("name") or "")
    if ss.read_mode(Path(workspace), name) == "manual" and approved_by not in ("user", "operator"):
        raise ApprovalExecError("a manual skill's edit needs its owner's approval")
    res = await asyncio.to_thread(
        ss.write_skill_edit, Path(workspace), name,
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
    (the gateway's ExecTool, with its own guards).

    ``deps_hash`` binds the approval to the skill's own declared specs, never
    to ``payload["specs"]`` (recomputing per-payload would let a tampered
    payload keep a matching hash). So the payload is checked against what the
    skill currently declares here, right before running it: a command that is
    not among them refuses instead of running something nobody reviewed."""
    if deps.exec_run is None:
        raise ApprovalExecError(
            "installing dependencies needs the running gateway's exec tool; "
            "approve it from the chat or the web UI")
    name = str(payload.get("name") or "")
    declared = (si.runnable_install_specs(Path(workspace) / "skills" / name)
                if ss._safe_name(name) else [])
    allowed_commands = {s["command"] for s in declared}
    specs = [s for s in (payload.get("specs") or [])
             if isinstance(s, dict) and s.get("command")]
    undeclared = [s["command"] for s in specs if s["command"] not in allowed_commands]
    if undeclared:
        raise ApprovalExecError(
            f"the skill no longer declares: {', '.join(undeclared)}")
    results = await si.run_install_specs(specs, exec_run=deps.exec_run)
    approval_id, approved_by = _approver(deps)
    si._audit(Path(workspace), name=payload.get("name"), action="install_deps",
              commands=[s["command"] for s in specs],
              succeeded=[r["command"] for r in results if r.get("success")],
              approval_id=approval_id, approved_by=approved_by)
    failed = [r["command"] for r in results if not r.get("success")]
    if failed:
        raise ApprovalExecError(f"install failed: {', '.join(failed)}")
    return {"ran": True, "results": results}


# --- the skills judge as an approver ---------------------------------------------

JudgeSettings = tuple[str, str, str]  # (trigger, model, max_severity)


def judge_settings(app_config: Any = None) -> JudgeSettings:
    """(trigger, model, max_severity) of ``skills.security.llm_judge`` from the
    given config, else from the loaded one."""
    try:
        j = app_config.skills.security.llm_judge
        return (str(j.trigger or "off"), str(j.model or ""), str(j.max_severity or "caution"))
    except AttributeError:
        return ss._import_judge()


def judge_sees_all(tree: Path, findings: list[dict]) -> bool:
    """True when the judge reads everything it would be clearing.

    ``judge_skill`` reads SKILL.md's full text and the files under
    ``scripts/`` (a plain top-level file named "scripts", not a directory,
    does not count — the judge never opens it), up to a character budget. A
    clearance is only worth something for content it actually read, so: every
    file is SKILL.md or under a ``scripts/`` directory, every one of those
    files decodes cleanly as text (a binary file comes back as replacement
    characters — that is not a read), every finding points at one of them, no
    install specs are declared (they live in the frontmatter, which the judge
    now reads, but a declared install command still runs outside anything the
    judge inspects), and nothing is cut at the budget."""
    from durin.security.skill_judge import _BODY_BUDGET, _gather_content

    tree = Path(tree)
    for p in tree.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(tree)
        posix = rel.as_posix()
        if posix == ".scan.json":
            continue
        under_scripts = len(rel.parts) > 1 and rel.parts[0] == "scripts"
        if posix != "SKILL.md" and not under_scripts:
            return False
        if not ss._is_text_bytes(p.read_bytes()):
            return False
    for f in findings:
        where = str(f.get("where") or "")
        if where != "SKILL.md" and not where.startswith("scripts/"):
            return False
    md = tree / "SKILL.md"
    if md.is_file():
        data, _ = split_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
        meta = data.get("metadata")
        if isinstance(meta, dict) and any(isinstance(b, dict) and b.get("install")
                                          for b in meta.values()):
            return False
    _name, content = _gather_content(tree)
    return len(content) <= _BODY_BUDGET


def _judge_tree(tree_cm: Callable[[], ContextManager[Path]], findings: list[dict],
                model: str, max_severity: str, llm_invoke: Any) -> str | None:
    """Run ``judge_skill`` over the tree the request would produce.

    Eligibility is a security boundary, so it is never taken on the caller's
    word: the tree is re-scanned deterministically here, and a tree that
    scans ``dangerous`` is not judge-eligible no matter what action, mode or
    scan the caller believed applied — a caller's mistaken or stale claim
    must not let the judge clear something a live re-scan would block."""
    from durin.security.skill_judge import judge_skill
    from durin.security.skill_scan import scan_skill

    invoke = llm_invoke
    if invoke is None:
        from durin.memory.llm_invoke import judge_llm_invoke
        invoke = judge_llm_invoke
    with tree_cm() as tree:
        if not judge_sees_all(tree, findings):
            return None
        if scan_skill(tree).verdict == "dangerous":
            return None
        outcome = judge_skill(tree, llm_invoke=invoke, model=model, max_severity=max_severity)
    # A "safe" verdict that still names a concrete problem is not a clearance:
    # only "safe" with no finding above info level clears the request.
    if outcome.verdict == "safe":
        return "safe" if all(f.severity == "info" for f in outcome.findings) else "caution"
    return outcome.verdict or "caution"


def _judge_fn(tree_cm: Callable[[], ContextManager[Path]], *, findings: list[dict],
              settings: JudgeSettings, llm_invoke: Any) -> approval.JudgeFn | None:
    trigger, model, max_severity = settings
    if trigger == "off":
        return None

    async def judge() -> str | None:
        return await asyncio.to_thread(_judge_tree, tree_cm, findings, model,
                                       max_severity, llm_invoke)

    return judge


def install_judge(qdir: Path, *, action: str, findings: list[dict], settings: JudgeSettings,
                  llm_invoke: Any = None) -> approval.JudgeFn | None:
    """The judge for a skill install, or None when it may not decide: only a
    ``confirm`` install is eligible (``block`` means dangerous, which only a
    person can accept), and only while the judge is enabled."""
    if action != "confirm":
        return None
    return _judge_fn(lambda: nullcontext(Path(qdir)), findings=findings,
                     settings=settings, llm_invoke=llm_invoke)


def edit_judge(workspace: Path, name: str, *, file: str, content: str,
               settings: JudgeSettings, llm_invoke: Any = None) -> approval.JudgeFn | None:
    """The judge for a skill edit, or None when it may not decide: only an
    ``auto`` skill (a ``manual`` skill's owner consents, not a model) whose
    post-edit scan is ``caution`` (never ``dangerous``). It reads a throwaway
    copy of the skill with the edit applied, never the live skill on disk.

    Eligibility is a security boundary, so the skill's mode and the scan are
    read here, from the live, pre-edit skill, rather than accepted as
    arguments: an edit that flips ``metadata.durin.mode`` to ``auto`` on a
    ``manual`` skill must not thereby make itself judge-eligible, and a stale
    or wrongly-computed scan the caller believed applied must not either."""
    skill_dir = ss._resolve_skill_dir(Path(workspace), name)
    if skill_dir is None or ss.read_mode(Path(workspace), name) != "auto":
        return None
    scan = ss.scan_skill_write(skill_dir, {file: content})
    if scan.after != "caution":
        return None
    return _judge_fn(lambda: ss.post_write_tree(skill_dir, {file: content}),
                     findings=scan.findings, settings=settings, llm_invoke=llm_invoke)


def register_all() -> None:
    """Register the three skill kinds with the approval executor registry."""
    register("skill_install", hash_fn=install_hash, execute_fn=execute_install)
    register("skill_edit", hash_fn=edit_hash, execute_fn=execute_edit)
    register("skill_deps", hash_fn=deps_hash, execute_fn=execute_deps)


register_all()
