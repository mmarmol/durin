"""Source resolution for skill import (§6.B). A source is rarely a direct
`.../SKILL.md`: a GitHub repo may hold many skills under subdirs, a local path
may be a directory of skills. `resolve_candidates` turns any source into a list
of concrete `SkillCandidate`s. The *mechanical* discovery is deterministic; the
*fuzzy* part ("which of many", "is this URL even a skill") is the agent's job —
fuzzy/unrecognized sources return an `unresolved_reason`, not an exception, so
the orchestrator skill can investigate and re-resolve.

No install, no scan here — pure discovery. Network fetches are SSRF-safe."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from durin.agent.skills_frontmatter import split_frontmatter

_GITHUB_API = "https://api.github.com"
_GITHUB_PREFIXES = ("github:", "https://github.com/", "http://github.com/")


@dataclass
class SkillCandidate:
    name: str           # skill name: frontmatter/dir name, or repo name at root
    ref: str            # concrete fetchable ref understood by fetch_candidate:
                        #   local:  absolute filesystem path to the skill dir
                        #   https:  direct URL to a SKILL.md
                        #   github: "github:owner/repo@branch/<dir>"
    kind: str           # "local" | "https" | "github"
    detail: str = ""    # description if known cheaply


@dataclass
class ResolveResult:
    candidates: list[SkillCandidate] = field(default_factory=list)
    unresolved_reason: str = ""   # non-empty => the agent must investigate


# --- local -------------------------------------------------------------------

def _name_of(skill_dir: Path) -> tuple[str, str]:
    try:
        data, _ = split_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
        return (str(data.get("name") or skill_dir.name), str(data.get("description") or ""))
    except OSError:
        return (skill_dir.name, "")


def _resolve_local(p: Path) -> ResolveResult:
    if p.is_file() and p.name == "SKILL.md":
        p = p.parent
    if (p / "SKILL.md").is_file():
        name, detail = _name_of(p)
        return ResolveResult([SkillCandidate(name, str(p.resolve()), "local", detail)])
    if p.is_dir():
        cands = []
        for md in sorted(p.glob("*/SKILL.md")):
            name, detail = _name_of(md.parent)
            cands.append(SkillCandidate(name, str(md.parent.resolve()), "local", detail))
        if cands:
            return ResolveResult(cands)
    return ResolveResult(unresolved_reason=f"no SKILL.md at or under {p}")


# --- github ------------------------------------------------------------------

def _is_github_url(url: str) -> bool:
    """True only for the real GitHub API / raw hosts (https). The token guard:
    we attach the GitHub token ONLY to these, never to an arbitrary https source,
    so a malicious/direct URL can't capture the credential."""
    return url.startswith("https://api.github.com/") or url.startswith(
        "https://raw.githubusercontent.com/")


def _github_token() -> str:
    """Resolve the configured GitHub token via durin secrets, or "" (anonymous).
    `skills.security.github_token_secret` holds a secret NAME; missing/empty
    degrades to anonymous (never raises)."""
    from durin.config.loader import load_config
    from durin.security.secrets import resolve_secret
    try:
        name = (load_config().skills.security.github_token_secret or "").strip()
        if not name:
            return ""
        return str(resolve_secret(f"${{secret:{name}}}") or "")
    except Exception:  # noqa: BLE001 — missing secret / store issue → anonymous
        return ""


def _gh_headers(url: str, accept: str | None = None) -> dict:
    """Headers for a GitHub request — attaches the token ONLY for GitHub hosts."""
    headers: dict = {}
    if accept:
        headers["Accept"] = accept
    if _is_github_url(url):
        tok = _github_token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    return headers


def _gh_get_json(url: str) -> dict:
    """GET a GitHub API URL as JSON over the SSRF-safe client. Runs the async
    fetch in a fresh thread so it works whether or not a loop is already running."""
    import asyncio
    import threading

    from durin.security.network import ssrf_safe_async_client

    box: dict = {}

    async def _go() -> dict:
        async with ssrf_safe_async_client() as client:
            resp = await client.get(url, headers=_gh_headers(url, "application/vnd.github+json"),
                                    timeout=15.0)
            resp.raise_for_status()
            return resp.json()

    def _run() -> None:
        try:
            box["value"] = asyncio.run(_go())
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller below
            box["error"] = exc

    t = threading.Thread(target=_run)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _parse_github(source: str) -> tuple[str, str, str | None, str]:
    """Return (owner, repo, branch_or_None, subpath). branch None => default."""
    branch: str | None = None
    if source.startswith("github:"):
        rest = source[len("github:"):]
        # optional @branch on the repo: owner/repo@branch/sub
        rest, _, frag = rest.partition("@")
        parts = [s for s in rest.split("/") if s]
        owner, repo = parts[0], parts[1]
        subpath = "/".join(parts[2:])
        if frag:
            fb = frag.split("/")
            branch = fb[0]
            if len(fb) > 1:
                subpath = "/".join(fb[1:])
        return owner, repo, branch, subpath
    # https://github.com/owner/repo[/tree/branch/sub...]
    tail = re.sub(r"^https?://github\.com/", "", source).strip("/")
    parts = [s for s in tail.split("/") if s]
    owner, repo = parts[0], parts[1]
    repo = re.sub(r"\.git$", "", repo)
    subpath = ""
    if len(parts) > 2 and parts[2] in ("tree", "blob"):
        branch = parts[3] if len(parts) > 3 else None
        subpath = "/".join(parts[4:])
    elif len(parts) > 2:
        subpath = "/".join(parts[2:])
    return owner, repo, branch, subpath


def _resolve_github(source: str) -> ResolveResult:
    owner, repo, branch, subpath = _parse_github(source)
    if branch is None:
        meta = _gh_get_json(f"{_GITHUB_API}/repos/{owner}/{repo}")
        branch = meta.get("default_branch") or "main"
    tree = _gh_get_json(f"{_GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
    sub = subpath.strip("/")

    skill_dirs: list[str] = []
    for entry in tree.get("tree", []):
        path = entry.get("path", "")
        if entry.get("type") != "blob" or not (path == "SKILL.md" or path.endswith("/SKILL.md")):
            continue
        skill_dirs.append(path[: -len("/SKILL.md")] if path.endswith("/SKILL.md") else "")

    def _mk(skill_dir: str) -> SkillCandidate:
        name = skill_dir.rsplit("/", 1)[-1] if skill_dir else repo
        return SkillCandidate(name, f"github:{owner}/{repo}@{branch}/{skill_dir}", "github")

    if sub:
        # Exact: the subpath IS a skill dir, or skills live under it.
        exact = [d for d in skill_dirs if d == sub or d.startswith(sub + "/")]
        if exact:
            return ResolveResult([_mk(d) for d in exact])
        # Fallback: a registry slug (e.g. a skills.sh skillId) is the skill's NAME,
        # not its repo path — match the last segment anywhere in the tree, since
        # skills live under varied prefixes (skills/, skills/.curated/, …).
        leaf = sub.rsplit("/", 1)[-1]
        named = [d for d in skill_dirs if d.rsplit("/", 1)[-1] == leaf]
        if named:
            return ResolveResult([_mk(d) for d in named])
        return ResolveResult(unresolved_reason=f"no SKILL.md found in github:{owner}/{repo}/{sub}")

    if skill_dirs:
        return ResolveResult([_mk(d) for d in skill_dirs])
    return ResolveResult(unresolved_reason=f"no SKILL.md found in github:{owner}/{repo}")


# --- entry point -------------------------------------------------------------

def resolve_candidates(source: str) -> ResolveResult:
    """Turn any import source into concrete skill candidates (or an
    unresolved_reason for the agent to investigate)."""
    source = source.strip()
    if not source:
        return ResolveResult(unresolved_reason="empty source")
    if source.startswith("clawhub:"):
        slug = source[len("clawhub:"):].strip().strip("/")
        if not slug:
            return ResolveResult(unresolved_reason="empty clawhub slug")
        return ResolveResult([SkillCandidate(slug, f"clawhub:{slug}", "clawhub")])
    if source.startswith(_GITHUB_PREFIXES):
        return _resolve_github(source)
    if re.match(r"^https?://", source):
        if source.rstrip("/").endswith("SKILL.md"):
            segs = [s for s in source.split("/") if s]
            name = segs[-2] if len(segs) >= 2 else "skill"
            return ResolveResult([SkillCandidate(name, source, "https")])
        return ResolveResult(unresolved_reason=(
            "URL is not a direct SKILL.md and is not a GitHub repo; "
            "ask the agent to investigate the link"))
    return _resolve_local(Path(source).expanduser())
