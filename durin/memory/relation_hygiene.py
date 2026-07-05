"""Dream pass: keep the entity graph's relation vocabulary tidy.

Two producers write typed relations — the dream's document/session extraction
and the agent's ``memory_upsert_entity`` — and without a shared vocabulary they
coin surface-form variants of the same edge (``occurs-in`` vs ``occurs_in``).
Walking the graph then treats those as different edges. New writes are already
canonicalised at the choke point (``field_patch.normalize_relation_type``); this
nightly pass fixes relations that predate that, re-keys their provenance, drops
the duplicates a rename exposes, and — crucially — **reports** the relation
vocabulary so an operator can see whether relations are being created cleanly.

It is deliberately deterministic: it only merges labels that differ in surface
form, never labels that differ in direction. Inverse pairs (``treats`` /
``treated_by``) are the same fact from both ends and are kept distinct — merging
them would flip facts. Folding genuine same-direction synonyms (e.g.
``is_ineffective_for`` ≈ ``unresponsive_to``) would need an LLM and is left out
until the vocabulary is large enough to warrant it; the telemetry here is what
tells us when that day comes.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Any

from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import normalize_relation_type
from durin.memory.field_provenance import relation_prov_key

__all__ = ["canonicalize_page_relations", "run_consolidate_relations_pass"]


def canonicalize_page_relations(page: EntityPage) -> tuple[bool, int]:
    """Rewrite one page's relation types to canonical surface form in place.

    Returns ``(changed, dropped)`` — whether anything changed, and how many
    relations collapsed into an existing one once their labels matched. Keeps
    the per-``(to, type)`` provenance in sync with the renamed edges."""
    new_rels: list[dict[str, Any]] = []
    seen: set[tuple[Any, str]] = set()
    changed = False
    dropped = 0
    for rel in page.relations or []:
        old = rel.get("type")
        canon = normalize_relation_type(old)
        if canon != old:
            changed = True
        key = (rel.get("to"), canon)
        if key in seen:  # a rename exposed a duplicate edge
            dropped += 1
            changed = True
            continue
        seen.add(key)
        new_rels.append({**rel, "type": canon})
    if not changed:
        return False, 0
    page.relations = new_rels

    prov = page.provenance or {}
    rel_prov = prov.get("relations")
    if isinstance(rel_prov, dict):
        rekeyed: dict[str, Any] = {}
        for entry in rel_prov.values():
            if not isinstance(entry, dict):
                continue
            to = entry.get("to")
            canon = normalize_relation_type(entry.get("type"))
            rekeyed[relation_prov_key(str(to), str(canon))] = {**entry, "type": canon}
        prov["relations"] = rekeyed
        page.provenance = prov
    return True, dropped


def run_consolidate_relations_pass(
    workspace: Path, *, max_seconds: int = 0,
) -> dict[str, Any]:
    """Canonicalise relation surface forms across every entity page, and report
    the relation vocabulary (supervision).

    Deterministic and idempotent: a second run over unchanged pages rewrites
    nothing. The returned counts (distinct types before/after, pages changed,
    duplicate edges merged) go to the cron log so a growing gap between
    ``types_before`` and ``types_after`` — or many merges — flags that relations
    are being created sloppily upstream."""
    started = time.monotonic()
    ents = Path(workspace) / "memory" / "entities"
    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    pages_changed = 0
    relations = 0
    merged = 0

    if ents.is_dir():
        for md in sorted(ents.rglob("*.md")):
            if max_seconds and (time.monotonic() - started) > max_seconds:
                break
            try:
                page = EntityPage.from_file(md)
            except Exception:  # noqa: BLE001
                continue
            if page is None or not page.relations:
                continue
            for rel in page.relations:
                before[str(rel.get("type"))] += 1
                relations += 1
            changed, dropped = canonicalize_page_relations(page)
            for rel in page.relations:
                after[str(rel.get("type"))] += 1
            merged += dropped
            if changed:
                try:
                    page.save(md)
                    pages_changed += 1
                except Exception:  # noqa: BLE001
                    pass

    return {
        "relations": relations,
        "pages_changed": pages_changed,
        "types_before": len(before),
        "types_after": len(after),
        "merged_duplicates": merged,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
