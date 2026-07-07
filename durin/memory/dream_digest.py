"""Shared mapping from dream/absorb telemetry events to digest activity items.

Two surfaces render "what the dream did" and they MUST agree:

* the dream-digest endpoint (``durin.service.memory``) reads persisted
  telemetry JSONL after the fact and lists past activity, and
* the live websocket tee (``durin.memory.dream_live``) maps the same events
  as the dream emits them, in real time, while a run is in progress.

Both call :func:`map_dream_event`, so a merge/discover/flagged item looks
identical whether it arrives live or is replayed from the digest. The mapping
returns plain dicts (``kind``/``summary``/``ref``/``ref_kind``/``at_ms``) — the
service layer wraps them in its ``DreamEvent`` DTO; the websocket tee sends them
as-is. Keeping the mapping here (the memory layer) avoids a channels/service
layering inversion and keeps it pure, so it is safe to call from a worker thread.
"""

from __future__ import annotations

from typing import Any

# Per-item activity events (each maps to one or more digest items).
DREAM_ACTIVITY_TYPES = frozenset({
    "memory.absorb.auto_merged",
    "memory.dream.discover",
    "memory.dream.skill_extract",
    "memory.dream.skill_signals",
    "memory.dream.learnings",
    "memory.dream.flagged",
    "memory.dream.parse_failure",
    "memory.dream.vector_unavailable",
    "memory.dream.run_summary",
    "skill.curation_action",
})

# Run-boundary markers (not activity items — they set the digest's last-run time).
RUN_MARKER_TYPES = frozenset({"memory.dream.start", "memory.dream.end"})

# Every dream/absorb telemetry type the digest cares about.
DREAM_EVENT_TYPES = DREAM_ACTIVITY_TYPES | RUN_MARKER_TYPES


def map_dream_event(event_type: str, data: dict[str, Any], at_ms: int) -> list[dict[str, Any]]:
    """Map one raw telemetry event to zero or more activity dicts.

    Each dict has ``kind`` ("merged" | "created" | "improved" | "flagged" |
    "warning" | "run"), a human ``summary``, an optional ``ref`` / ``ref_kind``
    deep-link target,
    and ``at_ms`` (epoch milliseconds). Pure and dependency-free — safe to call
    from any thread (the dream passes run in worker threads).
    """
    if event_type == "memory.absorb.auto_merged":
        canonical = data.get("canonical", "")
        absorbed = data.get("absorbed", "")
        return [{
            "kind": "merged",
            "summary": f"Merged {absorbed} → {canonical}",
            "ref": canonical or None,
            "ref_kind": "entity" if canonical else None,
            "at_ms": at_ms,
        }]

    if event_type == "memory.dream.discover":
        refs: list[str] = data.get("refs") or []
        written = data.get("written", len(refs))
        if not refs:
            if not written:
                return []  # pass ran but discovered nothing — not feed-worthy
            return [{
                "kind": "created",
                "summary": f"Discovered {written} entities",
                "ref": None,
                "ref_kind": None,
                "at_ms": at_ms,
            }]
        return [{
            "kind": "created",
            "summary": f"Discovered entity {ref}",
            "ref": ref,
            "ref_kind": "entity",
            "at_ms": at_ms,
        } for ref in refs]

    if event_type == "memory.dream.learnings":
        refs = data.get("refs") or []
        written = data.get("written", len(refs))
        if not refs:
            if not written:
                return []  # pass ran but logged nothing — not feed-worthy
            return [{
                "kind": "created",
                "summary": f"Logged {written} learnings",
                "ref": None,
                "ref_kind": None,
                "at_ms": at_ms,
            }]
        return [{
            "kind": "created",
            "summary": f"Logged learning {ref}",
            "ref": ref,
            "ref_kind": "entity",
            "at_ms": at_ms,
        } for ref in refs]

    if event_type == "memory.dream.skill_extract":
        touched = data.get("skills_touched", 0)
        if not touched:
            return []  # the skill-extract pass runs every dream; only surface
            #            it when it actually created a skill (avoids per-run noise)
        # skill-extract authors NEW skills (skill_write is create-only); the
        # curation pass is what improves existing skills. Label accordingly so
        # the feed matches the "skills nuevas" / "skills mejoradas" split.
        return [{
            "kind": "created",
            "summary": f"Created {touched} new skill(s) from session patterns",
            "ref": None,
            "ref_kind": "skill",
            "at_ms": at_ms,
        }]

    if event_type == "skill.curation_action":
        # What the curation pass DID to an existing skill — which skill and how.
        # Only applied actions are feed-worthy (skips/failures are noise). This is
        # what tells the operator that "1 skill improved" means "restructured
        # qr-code-reader", and is the surface that would have shown a bad change.
        if not data.get("applied"):
            return []
        action = str(data.get("action") or "")
        skill = str(data.get("skill") or "")
        verb = {"restructure": "Restructured", "evolve": "Improved",
                "fuse": "Fused into", "retire": "Retired",
                "backfill": "Repaired frontmatter of"}.get(action, "Curated")
        return [{
            "kind": "retired" if action == "retire" else "improved",
            "summary": f"{verb} skill `{skill}`" if skill else f"{verb} a skill",
            "ref": skill or None,
            "ref_kind": "skill" if skill else None,
            "at_ms": at_ms,
        }]

    if event_type == "memory.dream.skill_signals":
        skills: list[str] = data.get("skills") or []
        logged = data.get("logged", len(skills))
        if not skills:
            if not logged:
                return []  # no signals logged — not feed-worthy
            return [{
                "kind": "improved",
                "summary": f"Logged {logged} skill signal(s)",
                "ref": None,
                "ref_kind": "skill",
                "at_ms": at_ms,
            }]
        return [{
            "kind": "improved",
            "summary": f"Logged skill signal for {skill}",
            "ref": skill,
            "ref_kind": "skill",
            "at_ms": at_ms,
        } for skill in skills]

    if event_type == "memory.dream.flagged":
        canonical = data.get("canonical", "")
        return [{
            "kind": "flagged",
            "summary": "Flagged a memory pair for review",
            "ref": canonical or None,
            "ref_kind": "entity" if canonical else None,
            "at_ms": at_ms,
        }]

    if event_type == "memory.dream.parse_failure":
        # One warning per unparseable LLM response. These are rare in steady
        # state (json_repair absorbs formatting quirks); a run that produces
        # many of them means the dream model is misbehaving, and a loud feed
        # is exactly the signal the operator needs.
        stage = data.get("stage", "?")
        source = data.get("source") or ""
        is_entity_ref = ":" in source and "/" not in source
        summary = f"Dream output unparseable during {stage}"
        if source:
            summary += f" ({source})"
        return [{
            "kind": "warning",
            "summary": summary,
            "ref": source if is_entity_ref else None,
            "ref_kind": "entity" if is_entity_ref else None,
            "at_ms": at_ms,
        }]

    if event_type == "memory.dream.vector_unavailable":
        # Emitted once per run (dream_vector_index) when vector memory is
        # enabled but the backend is unavailable: semantic dedup degraded
        # to alias matching for this run.
        return [{
            "kind": "warning",
            "summary": "Dream ran without the vector index — semantic dedup degraded to alias matching",
            "ref": None,
            "ref_kind": None,
            "at_ms": at_ms,
        }]

    if event_type == "memory.dream.run_summary":
        # One entry per run, ALWAYS — so an empty run still shows "ran, nothing
        # new" rather than vanishing. Per-item detail is in the events above.
        sessions = data.get("sessions", 0)
        entities = data.get("entities", 0)
        merged = data.get("merged", 0)
        created = data.get("skills_created", 0)
        improved = data.get("skills_improved", 0)
        parts: list[str] = []
        if merged:
            parts.append(f"{merged} merge(s)")
        if entities:
            parts.append(f"{entities} entity update(s)")
        if created:
            parts.append(f"{created} new skill(s)")
        if improved:
            parts.append(f"{improved} skill edit(s)")
        if not parts and sessions:
            parts.append(f"processed {sessions} session(s)")
        detail = ", ".join(parts) if parts else "no new changes"
        return [{
            "kind": "run",
            "summary": f"Dream run — {detail}",
            "ref": None,
            "ref_kind": None,
            "at_ms": at_ms,
        }]

    return []
