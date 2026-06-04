"""§6.C acquire-on-gap — gated per-ref seed retrieval.

Given ONE registry ref the dream chose from a raw ``skill_search`` result, run the
§8.C gate and return its SKILL.md body ONLY if ``decide_action == 'allow'`` — else
None ("pick another"). The autonomous risk rule is enforced HERE, in code: the dream
(no human present) can never receive risky content. Cost-aware: a non-allowlisted ref
can never reach 'allow', so it is rejected INSTANTLY without a download; only
allowlisted (user-trusted) refs are fetched, and the gate's STATIC scan runs with the
LLM judge OFF. Path A (in-session, human present) does not use this — it drives the
raw tools and routes risky candidates to the user via ``ask_user_question``.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path


async def acquire_safe_seed(workspace, source: str, *, allowlist) -> dict | None:
    """Gate ONE registry ref for use as a seed. Returns
    ``{"name", "source", "content"}`` when the §8.C gate rates it ``allow``, else
    ``None``. Rejects a non-allowlisted ref without downloading it."""
    from durin.agent.skill_resolve import resolve_candidates
    from durin.agent.skills_import import (
        decide_action, fetch_candidate, validate_skill,
    )
    from durin.security.skill_scan import scan_skill

    allow = [p for p in (allowlist or []) if p]
    source = (source or "").strip()
    if not source:
        return None
    # Fast reject (no network): a non-allowlisted source can never reach 'allow'.
    if not any(source.startswith(p) for p in allow):
        return None

    res = resolve_candidates(source)
    if not res.candidates:
        return None
    cand = res.candidates[0]
    seed_root = Path(workspace) / ".durin" / "acquire-quarantine"
    try:
        qdir = await asyncio.to_thread(
            fetch_candidate, cand, quarantine_root=seed_root,
            allowlist=allow, judge_trigger="off")  # static scan only — never the judge
    except Exception:  # noqa: BLE001 — a bad candidate must not sink the caller
        return None
    try:
        vr = validate_skill(qdir)
        rep = scan_skill(qdir)
        action = decide_action(
            source, verdict=rep.verdict, carries_code=vr.carries_code, allowlist=allow)
        if action == "allow":
            body = (qdir / "SKILL.md").read_text(encoding="utf-8")
            return {"name": cand.name, "source": source, "content": body}
        return None
    finally:
        shutil.rmtree(qdir, ignore_errors=True)
