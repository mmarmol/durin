"""Skill import (§6.B) + validation. Deterministic, dependency-free where it
matters. The security SCAN lives in durin/security/skill_scan.py."""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import durin.agent.skill_resolve as _resolve
from durin.agent.skill_resolve import SkillCandidate
from durin.agent.skills_frontmatter import split_frontmatter
from durin.security.skill_judge import audit_skill
from durin.security.skill_scan import scan_skill

_VERDICT_ORDER = {"safe": 0, "caution": 1, "dangerous": 2}

_GITHUB_RAW = "https://raw.githubusercontent.com"
_DEFAULT_MAX_FILES = 100
_DEFAULT_MAX_TOTAL_BYTES = 3 * 1024 * 1024
_DEFAULT_MAX_FILE_BYTES = 1024 * 1024

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")


@dataclass
class ValidationReport:
    name: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    carries_code: bool = False
    code_artifacts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_skill(skill_dir: Path) -> ValidationReport:
    """Validate a skill dir against agentskills.io + detect code. name/description
    missing are ERRORS; name-shape issues are WARNINGS (import-friendly)."""
    skill_dir = Path(skill_dir)
    md = skill_dir / "SKILL.md"
    rep = ValidationReport(name=skill_dir.name)
    if not md.is_file():
        rep.errors.append("no SKILL.md")
        return rep
    data, _ = split_frontmatter(md.read_text(encoding="utf-8"))
    name = str(data.get("name") or "").strip()
    desc = str(data.get("description") or "").strip()
    if name:
        rep.name = name
        if not _NAME_RE.match(name):
            rep.warnings.append(f"name {name!r} not agentskills.io-conformant (1-64 lowercase/digits/hyphens)")
        if name != skill_dir.name:
            rep.warnings.append(f"name {name!r} != directory {skill_dir.name!r}")
    else:
        rep.errors.append("missing required 'name'")
    if not desc:
        rep.errors.append("missing required 'description'")
    elif len(desc) > 1024:
        rep.warnings.append("description exceeds 1024 chars")
    scripts = skill_dir / "scripts"
    if scripts.is_dir():
        for p in sorted(scripts.rglob("*")):
            if p.is_file():
                rep.code_artifacts.append(str(p.relative_to(skill_dir)))
    meta = data.get("metadata")
    if isinstance(meta, dict):
        for vendor, blob in meta.items():
            if isinstance(blob, dict) and blob.get("install"):
                rep.code_artifacts.append(f"metadata.{vendor}.install")
    rep.carries_code = bool(rep.code_artifacts)
    return rep


def decide_action(source: str, *, verdict: str, carries_code: bool, allowlist: list[str]) -> str:
    """§8.C trust×verdict gate. Returns 'allow' | 'confirm' | 'block'.
    'block' needs an explicit override; 'confirm' needs confirmation. The
    dangerous-block and carries-code-confirm have no opt-out; only the source
    check is loosened by the allowlist."""
    if verdict == "dangerous":
        return "block"
    allowlisted = any(source.startswith(p) for p in allowlist if p)
    if carries_code or verdict == "caution" or not allowlisted:
        return "confirm"
    return "allow"


# --- fetch into quarantine ---------------------------------------------------

def _http_get_bytes(url: str) -> bytes:
    """GET raw bytes over the SSRF-safe client (thread-bridged like the resolver)."""
    import asyncio
    import threading

    from durin.security.network import ssrf_safe_async_client

    box: dict = {}

    async def _go() -> bytes:
        async with ssrf_safe_async_client() as client:
            # _gh_headers attaches the GitHub token only for raw.githubusercontent
            # / api.github.com — never for a direct (non-GitHub) https source.
            resp = await client.get(url, headers=_resolve._gh_headers(url), timeout=30.0)
            resp.raise_for_status()
            return resp.content

    def _run() -> None:
        try:
            box["value"] = asyncio.run(_go())
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller
            box["error"] = exc

    t = threading.Thread(target=_run)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _parse_github_ref(ref: str) -> tuple[str, str, str, str]:
    """`github:owner/repo@branch/dir` -> (owner, repo, branch, dir)."""
    body = ref[len("github:"):] if ref.startswith("github:") else ref
    repo_part, _, branch_part = body.partition("@")
    owner, repo = [s for s in repo_part.split("/") if s][:2]
    bsegs = branch_part.split("/")
    branch = bsegs[0] or "main"
    skill_dir = "/".join(bsegs[1:]).strip("/")
    return owner, repo, branch, skill_dir


def _safe_rel(rel: str) -> bool:
    return bool(rel) and ".." not in Path(rel).parts and not Path(rel).is_absolute()


def _write(qdir: Path, rel: str, data: bytes, budget: list[int],
           caps: tuple[int, int, int]) -> None:
    max_files, max_total, max_file = caps
    if not _safe_rel(rel):
        raise ValueError(f"unsafe path in skill: {rel!r}")
    if len(data) > max_file:
        raise ValueError(f"file {rel!r} exceeds per-file cap ({len(data)} > {max_file} bytes)")
    budget[0] += 1
    budget[1] += len(data)
    if budget[0] > max_files or budget[1] > max_total:
        raise ValueError("skill exceeds import size/file caps")
    dest = qdir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _fetch_github(cand: SkillCandidate, qdir: Path, budget: list[int],
                  caps: tuple[int, int, int]) -> None:
    owner, repo, branch, skill_dir = _parse_github_ref(cand.ref)
    tree = _resolve._gh_get_json(
        f"{_resolve._GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
    prefix = (skill_dir + "/") if skill_dir else ""
    found = False
    for entry in tree.get("tree", []):
        path = entry.get("path", "")
        if entry.get("type") != "blob" or not path.startswith(prefix):
            continue
        rel = path[len(prefix):]
        if not rel:
            continue
        data = _http_get_bytes(f"{_GITHUB_RAW}/{owner}/{repo}/{branch}/{path}")
        _write(qdir, rel, data, budget, caps)
        found = True
    if not found:
        raise ValueError(f"no files under {cand.ref}")


def fetch_candidate(cand: SkillCandidate, *, quarantine_root: Path,
                    max_files: int = _DEFAULT_MAX_FILES,
                    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
                    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
                    judge_enabled: bool = False, judge_model: str = "",
                    judge_max_severity: str = "caution") -> Path:
    """Download one resolved candidate into `<quarantine_root>/<name>/`, run the
    §8.C audit (deterministic scan + optional LLM judge), and drop a `.scan.json`
    (source + merged verdict + findings) beside it. The downloaded tree is NOT
    installed — it sits in quarantine for the gate. Caps (config-driven) bound the
    total/per-file size and file count."""
    quarantine_root = Path(quarantine_root)
    caps = (max_files, max_total_bytes, max_file_bytes)
    qdir = quarantine_root / cand.name
    if qdir.exists():
        shutil.rmtree(qdir)
    qdir.mkdir(parents=True)
    budget = [0, 0]  # [files, bytes]
    if cand.kind == "local":
        src = Path(cand.ref)
        for p in sorted(src.rglob("*")):
            if p.is_file() and ".git" not in p.parts and p.name != ".scan.json":
                _write(qdir, str(p.relative_to(src)), p.read_bytes(), budget, caps)
    elif cand.kind == "https":
        _write(qdir, "SKILL.md", _http_get_bytes(cand.ref), budget, caps)
    elif cand.kind == "github":
        _fetch_github(cand, qdir, budget, caps)
    else:
        raise ValueError(f"unknown candidate kind: {cand.kind!r}")
    rep = audit_skill(qdir, judge_enabled=judge_enabled, judge_model=judge_model,
                      judge_max_severity=judge_max_severity)
    (qdir / ".scan.json").write_text(json.dumps({
        "source": cand.ref,
        "verdict": rep.verdict,
        "findings": [{"category": f.category, "severity": f.severity,
                      "where": f.where, "detail": f.detail} for f in rep.findings],
    }), encoding="utf-8")
    return qdir


# --- install (the gate invariant) --------------------------------------------

class SkillImportRefused(Exception):
    """install_imported_skill refused the install. `.action` is the gate verdict
    ('block' | 'confirm' | 'invalid' | 'exists'); `.verdict` is the §8.C verdict."""

    def __init__(self, action: str, verdict: str, message: str):
        super().__init__(message)
        self.action = action
        self.verdict = verdict


def _content_hash(skill_dir: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file() and p.name != ".scan.json":
            h.update(p.relative_to(skill_dir).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _audit(workspace: Path, **fields) -> None:
    log = Path(workspace) / ".durin" / "import-audit.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(fields) + "\n")


def _safe_qname(name: str) -> bool:
    return bool(name) and ".." not in name and re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name) is not None


def install_imported_skill(workspace: Path, quarantine_dir: Path, *, source: str,
                           allowlist: list[str], confirmed: bool = False,
                           override: bool = False, replace: bool = False) -> dict:
    """Install a quarantined skill — but ONLY through the §8.C gate, enforced
    HERE in code (not in the tool/skill/UI): `block` (dangerous) needs
    `override`; `confirm` (code / caution / out-of-allowlist) needs `confirmed`
    or `override`; a name that already exists needs `replace`. On pass: copy out
    of quarantine, stamp metadata.durin.provenance + mode=manual, commit, index,
    append the audit log, and consume the quarantine dir. Raises
    SkillImportRefused otherwise."""
    from durin.agent.skills_store import (
        _skill_md,
        _store_init,
        _sync_index,
        _today,
        _update_md,
        ensure_durin,
    )

    workspace = Path(workspace)
    quarantine_dir = Path(quarantine_dir)
    vr = validate_skill(quarantine_dir)
    if not vr.ok:
        raise SkillImportRefused("invalid", "", f"invalid skill: {vr.errors}")
    rep = scan_skill(quarantine_dir)  # fresh deterministic — the block path never trusts cache
    verdict = rep.verdict
    # Fold in the cached judge verdict (caps at caution → can raise to a confirm,
    # never enable a block; the fresh deterministic re-scan above owns blocking).
    sj = quarantine_dir / ".scan.json"
    if sj.is_file():
        try:
            stored = str(json.loads(sj.read_text()).get("verdict", ""))
            if _VERDICT_ORDER.get(stored, 0) > _VERDICT_ORDER.get(verdict, 0):
                verdict = stored
        except Exception:  # noqa: BLE001
            pass
    action = decide_action(source, verdict=verdict,
                           carries_code=vr.carries_code, allowlist=allowlist)
    if action == "block" and not override:
        raise SkillImportRefused("block", verdict,
                                 "dangerous verdict; explicit override required")
    if action == "confirm" and not (confirmed or override):
        raise SkillImportRefused("confirm", verdict,
                                 "confirmation required (carries code / caution / out-of-allowlist)")
    name = vr.name
    dest = _skill_md(workspace, name).parent
    if dest.exists():
        if not replace:
            raise SkillImportRefused("exists", verdict, f"skill already exists: {name}")
        shutil.rmtree(dest)

    store = _store_init(workspace)
    shutil.copytree(quarantine_dir, dest,
                    ignore=shutil.ignore_patterns(".scan.json", ".git"))
    chash = _content_hash(dest)

    def _stamp(data: dict) -> None:
        durin = ensure_durin(data)
        durin["mode"] = "manual"
        durin["provenance"] = {
            "source": source,
            "verdict": verdict,
            "confirmed": bool(confirmed),
            "overridden": bool(override),
            "replaced": bool(replace),
            "content_hash": chash,
            "created_at": _today(),
        }

    _update_md(dest / "SKILL.md", _stamp)
    sha = store.auto_commit(f"skill({name}): import from {source} [{verdict}]")
    _sync_index(workspace, name)
    _audit(workspace, name=name, source=source, verdict=verdict, action=action,
           confirmed=bool(confirmed), overridden=bool(override), replaced=bool(replace),
           content_hash=chash, commit=sha)
    shutil.rmtree(quarantine_dir, ignore_errors=True)  # consumed
    return {"ok": True, "name": name, "verdict": verdict, "commit": sha}


def reject_quarantined(workspace: Path, name: str) -> dict:
    """Discard a quarantined skill (delete its dir). The opposite of approve."""
    if not _safe_qname(name):
        return {"error": "invalid name"}
    qdir = Path(workspace) / ".durin" / "import-quarantine" / name
    if not qdir.is_dir():
        return {"error": f"not in quarantine: {name}"}
    shutil.rmtree(qdir, ignore_errors=True)
    return {"ok": True, "name": name}


def declared_install_specs(skill_dir: Path) -> list[str]:
    """Human-readable list of a skill's declared dependency installs (e.g.
    ``brew: gh``). INFO ONLY — durin never auto-runs them (policy 'never' in v1,
    spec B11). Surfaces what the user/agent would need to install themselves."""
    md = Path(skill_dir) / "SKILL.md"
    if not md.is_file():
        return []
    try:
        data, _ = split_frontmatter(md.read_text(encoding="utf-8"))
    except OSError:
        return []
    meta = data.get("metadata")
    out: list[str] = []
    if isinstance(meta, dict):
        for blob in meta.values():
            specs = blob.get("install") if isinstance(blob, dict) else None
            if not isinstance(specs, list):
                continue
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                kind = str(spec.get("kind", "?"))
                val = str(spec.get("formula") or spec.get("package")
                          or spec.get("module") or spec.get("cask") or spec.get("url") or "")
                out.append(f"{kind}: {val}" if val else kind)
    return out


def trust_prefix_for(ref: str) -> str:
    """Suggest a starting allowlist prefix to pre-fill for 'trust this source'.
    The user edits it (e.g. broaden a repo to the whole org). NOT a repo-vs-org
    decision — just a sensible, specific starting point per source kind:
    github → the repo (`github:owner/repo`); https → the SKILL.md's dir; local →
    the path as-is."""
    ref = (ref or "").strip()
    if ref.startswith("github:"):
        repo_part = ref[len("github:"):].split("@", 1)[0]
        segs = [s for s in repo_part.split("/") if s][:2]
        return "github:" + "/".join(segs) if len(segs) == 2 else ref
    if ref.startswith(("https://", "http://")):
        if ref.rstrip("/").endswith("SKILL.md"):
            return ref.rsplit("/", 1)[0] + "/"
        return ref
    return ref
