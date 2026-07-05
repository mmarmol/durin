"""A FieldPatch is one structured edit the agent or dream emits for an entity.

Applied with precedence against the page's existing per-field provenance.
The agent emits name/aliases/relations/body patches; the dream
emits attribute patches (decision b). Apply records provenance per field.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from durin.memory.entity_page import EntityPage
from durin.memory.field_provenance import (
    coerce_relation_prov,
    incoming_wins,
    make_entry,
    relation_prov_key,
)

__all__ = ["FieldPatch", "apply_field_patch", "normalize_relation_type"]

_REL_TYPE_NONWORD = re.compile(r"[^a-z0-9]+")


def normalize_relation_type(rtype: Any) -> str:
    """Canonical surface form of a relation-type label.

    Lowercase; any run of non-alphanumeric characters becomes a single
    underscore; leading/trailing underscores trimmed. So ``occurs-in``,
    ``Occurs In`` and ``occurs_in`` all become ``occurs_in``. This merges
    spelling/format variants only — it never changes a relation's direction or
    meaning (``treats`` and ``treated_by`` stay distinct). An empty / non-string
    input yields ``""``, which the caller may keep as an untyped relation."""
    return _REL_TYPE_NONWORD.sub("_", str(rtype or "").lower()).strip("_")

PatchKind = Literal[
    "attribute", "relation", "alias", "body_append", "body_replace", "derived_from",
]


def _append_body(page: EntityPage, patch: FieldPatch) -> None:
    """Append an attributed section to the body (the original body_append)."""
    sep = "\n\n" if page.body and not page.body.endswith("\n") else "\n"
    marker = f"<!-- {patch.author} {patch.source_ref} -->"
    page.body = (page.body + sep + marker + "\n" + str(patch.value)).rstrip("\n")


def _record_body_authority(
    page: EntityPage, prov: dict[str, Any], entry: dict[str, Any]
) -> None:
    """Track the highest-authority body contributor in ``prov["body"]``.

    Never downgrades: an agent append over a user-authored body keeps the body
    owned by ``user`` (so a later agent ``body_replace`` cannot win and clobber
    it). Mirrors the per-field precedence used for attributes/relations.
    """
    if incoming_wins(existing=prov.get("body"), incoming=entry):
        prov["body"] = entry
    page.provenance = prov


@dataclass
class FieldPatch:
    """One structured edit. ``author`` may be None at construction and is
    resolved by ``memory_writer`` from the ambient ``author_scope``."""

    kind: PatchKind
    source_ref: str
    at: datetime
    author: str | None = None
    key: str | None = None       # attribute key
    value: Any = None            # attribute value / relation dict / alias str / body text


def apply_field_patch(page: EntityPage, patch: FieldPatch) -> bool:
    """Apply one patch to ``page`` in place, respecting precedence.

    Returns True if the page changed. Records per-field provenance for
    attribute/relation patches. ``patch.author`` must be resolved (non-None)
    before calling — the writer does that from the author scope.
    """
    if patch.author is None:
        raise ValueError("patch.author must be resolved before apply")
    entry = make_entry(source_ref=patch.source_ref, author=patch.author, at=patch.at)
    prov = page.provenance or {}

    if patch.kind == "attribute":
        if patch.key is None:
            raise ValueError("attribute patch needs a key")
        existing = (prov.get("attributes") or {}).get(patch.key)
        if not incoming_wins(existing=existing, incoming=entry):
            return False
        page.attributes[patch.key] = patch.value
        prov.setdefault("attributes", {})[patch.key] = entry
        page.provenance = prov
        return True

    if patch.kind == "relation":
        to = patch.value.get("to")
        # Canonicalise the label at the write choke point so spelling/format
        # variants (``occurs-in`` / ``Occurs In`` / ``occurs_in``) collapse to
        # one — every producer (dream, agent tool, extract) flows through here,
        # so the graph never accrues duplicate edges that differ only in surface
        # form. Direction and meaning are untouched.
        rtype = normalize_relation_type(patch.value.get("type"))
        for r in page.relations:                      # dedup by (to, type)
            if r.get("to") == to and r.get("type") == rtype:
                return False
        page.relations.append({**patch.value, "type": rtype})
        # Q1: provenance keyed by (to, type), not positional index — merges
        # cleanly.
        rel_prov = coerce_relation_prov(prov.get("relations"))
        rel_prov[relation_prov_key(str(to), str(rtype))] = {
            "to": to, "type": rtype, **entry,
        }
        prov["relations"] = rel_prov
        page.provenance = prov
        return True

    if patch.kind == "derived_from":
        ref = patch.value if isinstance(patch.value, str) else str(patch.value)
        # Per-link provenance keyed by the ref string (merge-safe, unlike the
        # index-keyed relation provenance). Precedence like attributes.
        df_prov = prov.get("derived_from") or {}
        existing = df_prov.get(ref)
        if ref in page.derived_from:
            # Already linked; only a higher-authority/newer writer re-stamps.
            # An identical entry is a true duplicate → no change.
            if existing == entry or not incoming_wins(existing=existing, incoming=entry):
                return False
        else:
            page.derived_from.append(ref)
        df_prov[ref] = entry
        prov["derived_from"] = df_prov
        page.provenance = prov
        return True

    if patch.kind == "alias":
        if patch.value in page.aliases:
            return False
        page.aliases.append(patch.value)
        return True

    if patch.kind == "body_append":
        _append_body(page, patch)
        _record_body_authority(page, prov, entry)
        return True

    if patch.kind == "body_replace":
        # Replace the whole body — but only if the incoming writer wins the
        # body's per-field precedence (user > dream > agent; same rank → newer).
        # On a win, set clean canonical prose with no append marker; git history
        # preserves the prior body either way. On a loss (e.g. an agent trying
        # to overwrite a user-authored body), degrade to a lossless append so
        # the new prose is not dropped and the higher authority is preserved.
        if incoming_wins(existing=prov.get("body"), incoming=entry):
            page.body = str(patch.value).rstrip("\n")
            prov["body"] = entry
            page.provenance = prov
            return True
        _append_body(page, patch)
        _record_body_authority(page, prov, entry)
        return True

    raise ValueError(f"unknown patch kind {patch.kind!r}")
