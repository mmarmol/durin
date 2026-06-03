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
from durin.security.skill_scan import scan_skill

_GITHUB_RAW = "https://raw.githubusercontent.com"
_MAX_FILES = 200
_MAX_BYTES = 5 * 1024 * 1024

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
            resp = await client.get(url, timeout=30.0)
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


def _write(qdir: Path, rel: str, data: bytes, budget: list[int]) -> None:
    if not _safe_rel(rel):
        raise ValueError(f"unsafe path in skill: {rel!r}")
    budget[0] += 1
    budget[1] += len(data)
    if budget[0] > _MAX_FILES or budget[1] > _MAX_BYTES:
        raise ValueError("skill exceeds import size/file caps")
    dest = qdir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _fetch_github(cand: SkillCandidate, qdir: Path, budget: list[int]) -> None:
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
        _write(qdir, rel, data, budget)
        found = True
    if not found:
        raise ValueError(f"no files under {cand.ref}")


def fetch_candidate(cand: SkillCandidate, *, quarantine_root: Path) -> Path:
    """Download one resolved candidate into `<quarantine_root>/<name>/`, run the
    §8.C scan, and drop a `.scan.json` (source + verdict + findings) beside it.
    The downloaded tree is NOT installed — it sits in quarantine for the gate."""
    quarantine_root = Path(quarantine_root)
    qdir = quarantine_root / cand.name
    if qdir.exists():
        shutil.rmtree(qdir)
    qdir.mkdir(parents=True)
    budget = [0, 0]  # [files, bytes]
    if cand.kind == "local":
        src = Path(cand.ref)
        for p in sorted(src.rglob("*")):
            if p.is_file() and ".git" not in p.parts and p.name != ".scan.json":
                _write(qdir, str(p.relative_to(src)), p.read_bytes(), budget)
    elif cand.kind == "https":
        _write(qdir, "SKILL.md", _http_get_bytes(cand.ref), budget)
    elif cand.kind == "github":
        _fetch_github(cand, qdir, budget)
    else:
        raise ValueError(f"unknown candidate kind: {cand.kind!r}")
    rep = scan_skill(qdir)
    (qdir / ".scan.json").write_text(json.dumps({
        "source": cand.ref,
        "verdict": rep.verdict,
        "findings": [{"category": f.category, "severity": f.severity,
                      "where": f.where, "detail": f.detail} for f in rep.findings],
    }), encoding="utf-8")
    return qdir
