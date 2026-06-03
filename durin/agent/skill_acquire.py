"""§6.C acquire-on-gap — gated seed acquisition.

Search the configured registries for prior art, fetch the top candidate the §8.C
gate rates ``allow``, and return its SKILL.md body as a seed. The autonomous risk
rule is enforced HERE, in code: a ``confirm``/``block`` hit is never returned, so
the dream (no human present) can only ever seed from a risk-free source. Path A
(in-session, human present) does not use this — it drives the raw tools and routes
risky candidates to the user via ``ask_user_question``.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path


async def acquire_safe_seed(
    workspace,
    query: str,
    *,
    registries,
    allowlist,
    limit: int = 10,
) -> dict | None:
    """Return ``{"name", "source", "content"}`` for the top risk-free registry hit
    matching ``query``, or ``None`` when no hit clears the §8.C gate."""
    from durin.agent.skill_registry import build_adapters, search_registries
    from durin.agent.skill_resolve import resolve_candidates
    from durin.agent.skills_import import (
        decide_action, fetch_candidate, validate_skill,
    )
    from durin.security.skill_scan import scan_skill

    allow = list(allowlist or [])
    query = (query or "").strip()
    if not query:
        return None

    hits = await search_registries(
        query, adapters=build_adapters(registries), allowlist=allow, limit=limit)
    seed_root = Path(workspace) / ".durin" / "acquire-quarantine"

    for hit in hits:
        res = resolve_candidates(hit.ref)
        if not res.candidates:
            continue
        cand = res.candidates[0]
        try:
            qdir = await asyncio.to_thread(
                fetch_candidate, cand, quarantine_root=seed_root, allowlist=allow)
        except Exception:  # noqa: BLE001 — a bad candidate must not sink the rest
            continue
        try:
            vr = validate_skill(qdir)
            rep = scan_skill(qdir)
            action = decide_action(
                hit.ref, verdict=rep.verdict,
                carries_code=vr.carries_code, allowlist=allow)
            if action == "allow":
                body = (qdir / "SKILL.md").read_text(encoding="utf-8")
                return {"name": hit.name, "source": hit.ref, "content": body}
        finally:
            shutil.rmtree(qdir, ignore_errors=True)
    return None
