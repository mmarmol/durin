"""Rename an entity page and redirect every reference to it.

An entity's key is its ``<type>:<slug>`` ref, and the slug is the page's
filename. Other files point at that key: live entity pages through
``relations[].to``, memory entries (episodic, stable, …) through their
``entities:`` list, and archived pages through ``archived_into``. Changing the
key therefore has to rewrite all of them, or the graph is left with dangling
edges.

:func:`collect_ref_rewrites` computes the rewrites of the committed files —
live and archived entity pages — as file contents without touching disk, so
both the rename and the entity merge (which redirects the absorbed page's
inbound references to the canonical) commit them atomically with their own
file operations through ``write_files_cas``. Memory entries are outside the
memory git history; :func:`redirect_entry_refs` rewrites their tags in place
right after the commit.

A rename keeps the old slug and the old display name as aliases, so anything
that found the entity by its previous key or name still finds it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from durin.memory.entity_page import EntityPage

__all__ = [
    "EntityRenameError",
    "RenameResult",
    "check_new_key",
    "collect_ref_rewrites",
    "redirect_entry_refs",
    "rename_entity",
    "validate_new_slug",
]

logger = logging.getLogger(__name__)

# A slug is a filename: lowercase letters/digits plus '-' and '_', starting
# with a letter or digit. Existing pages use both separators, so both are
# accepted; nothing else can reach the filesystem unescaped.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")


class EntityRenameError(Exception):
    """The rename cannot proceed (bad slug, missing page, taken key)."""


@dataclass
class RenameResult:
    old_ref: str
    new_ref: str
    sha: str | None
    rewritten: list[str] = field(default_factory=list)


def validate_new_slug(slug: str) -> str:
    """Return ``slug`` stripped, or raise :class:`EntityRenameError`."""
    s = (slug or "").strip()
    if not _SLUG_RE.match(s):
        raise EntityRenameError(
            f"invalid slug {slug!r}: use lowercase letters, digits, '-' or '_' "
            "(max 80 chars, starting with a letter or digit)")
    return s


def _split(ref: str) -> tuple[str, str]:
    if ":" not in ref:
        raise EntityRenameError(f"bad entity ref {ref!r}: expected '<type>:<slug>'")
    t, _, s = ref.partition(":")
    return t, s


def _rewrite_page_relations(page: EntityPage, old_ref: str, new_ref: str,
                            self_ref: str) -> bool:
    """Point this page's relations at ``new_ref`` instead of ``old_ref``.

    Duplicates that the redirect creates collapse to one edge, and an edge
    that would now point at the page itself is dropped. Relation provenance
    is keyed by ``(to, type)``, so its keys follow the rewrite."""
    from durin.memory.field_provenance import (
        _REL_KEY_SEP,
        coerce_relation_prov,
        relation_prov_key,
    )

    if not any(r.get("to") == old_ref for r in page.relations):
        return False
    prov = dict(page.provenance or {})
    rel_prov = coerce_relation_prov(prov.get("relations"))
    out: list[dict] = []
    seen: set[tuple] = set()
    # Provenance of edges that do not name the old key goes in first, so when
    # a redirected edge collapses into an existing one the existing edge keeps
    # its own provenance.
    new_prov: dict = {k: v for k, v in rel_prov.items()
                      if k.split(_REL_KEY_SEP, 1)[0] != old_ref}
    for r in page.relations:
        to, rtype = r.get("to"), r.get("type")
        old_key = relation_prov_key(to, rtype)
        if to == old_ref:
            to = new_ref
        if to == self_ref:
            continue
        key = (to, rtype)
        if key in seen:
            continue
        seen.add(key)
        out.append({**r, "to": to})
        if old_key in rel_prov:
            entry = rel_prov[old_key]
            if isinstance(entry, dict) and entry.get("to") == old_ref:
                entry = {**entry, "to": to}
            new_prov.setdefault(relation_prov_key(to, rtype), entry)
    page.relations = out
    if new_prov or "relations" in prov:
        prov["relations"] = new_prov
        page.provenance = prov
    return True


def collect_ref_rewrites(
    workspace: Path,
    old_ref: str,
    new_ref: str,
    *,
    skip: set[str] | None = None,
    originals: dict[str, bytes] | None = None,
) -> dict[str, bytes]:
    """Every committed file under ``memory/`` (live and archived entity pages)
    that references ``old_ref``, rewritten to reference ``new_ref`` — as
    ``{rel_path: new bytes}``, nothing written. Memory entries are not in the
    memory git history; :func:`redirect_entry_refs` rewrites them after the
    commit.

    ``skip`` holds rel paths the caller rewrites itself (the renamed page, the
    merge's canonical and absorbed pages). Unparseable files are left alone:
    a rename must never be blocked by one broken page. ``originals``, when
    given, receives the bytes each rewritten file had, for the commit's
    stale-content check (``write_files_cas(expect=…)``)."""
    memory = Path(workspace) / "memory"
    skip = skip or set()
    out: dict[str, bytes] = {}

    # Live entity pages: relation targets.
    ent_root = memory / "entities"
    if ent_root.is_dir():
        for md in sorted(ent_root.rglob("*.md")):
            rel = md.relative_to(memory).as_posix()
            if rel in skip or "archive" in md.relative_to(ent_root).parts:
                continue
            try:
                raw = md.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if old_ref not in text:
                continue
            page = EntityPage.from_text(text)
            if page is None:
                continue
            self_ref = f"{page.type}:{EntityPage.slug_from_path(md)}"
            if _rewrite_page_relations(page, old_ref, new_ref, self_ref):
                out[rel] = page.to_markdown().encode("utf-8")
                if originals is not None:
                    originals[rel] = raw

    # Archived entity pages: the pointer to the page they were merged into.
    arch_root = memory / "archive" / "entities"
    if arch_root.is_dir():
        for md in sorted(arch_root.rglob("*.md")):
            rel = md.relative_to(memory).as_posix()
            if rel in skip:
                continue
            try:
                raw = md.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if old_ref not in text:
                continue
            page = EntityPage.from_text(text)
            if page is None:
                continue
            changed = False
            for key in ("archived_into", "absorbed_into"):
                if page.extra.get(key) == old_ref:
                    page.extra[key] = new_ref
                    changed = True
            self_ref = f"{page.type}:{EntityPage.slug_from_path(md)}"
            if _rewrite_page_relations(page, old_ref, new_ref, self_ref):
                changed = True
            if changed:
                # Archived pages are only written by merges and renames and are
                # never auto-committed from hand edits, so they carry no
                # stale-content expectation (a dirty archive would block forever).
                out[rel] = page.to_markdown().encode("utf-8")

    return out


def redirect_entry_refs(workspace: Path, old_ref: str, new_ref: str) -> list[str]:
    """Point memory entries' ``entities:`` tags at ``new_ref`` instead of
    ``old_ref``; returns the rel paths rewritten.

    Entries (episodic, stable, …) live outside the memory git history — only
    entity pages and their archive are committed — so they are rewritten in
    place, atomically per file, after the key change is committed. Best-effort
    per entry: an unreadable one is skipped, never blocks the rename."""
    from durin.memory.paths import MEMORY_CLASSES
    from durin.memory.storage import load_entry, save_entry

    memory = Path(workspace) / "memory"
    done: list[str] = []
    for class_name in MEMORY_CLASSES:
        class_dir = memory / class_name
        if not class_dir.is_dir():
            continue
        for md in sorted(class_dir.glob("*.md")):
            try:
                if old_ref not in md.read_text(encoding="utf-8"):
                    continue
                entry = load_entry(md)
            except Exception:  # noqa: BLE001 — a broken entry never blocks a rename
                continue
            if old_ref not in entry.entities:
                continue
            ents: list[str] = []
            for e in entry.entities:
                e = new_ref if e == old_ref else e
                if e not in ents:
                    ents.append(e)
            entry.entities = ents
            try:
                save_entry(entry, md)
            except OSError as exc:
                logger.warning("redirect %s: could not rewrite %s: %s", old_ref, md, exc)
                continue
            done.append(md.relative_to(memory).as_posix())
    return done


def check_new_key(workspace: Path, ref: str, new_slug: str) -> str | None:
    """Raise :class:`EntityRenameError` unless ``ref`` may move to ``new_slug``.

    The key must not be a live page nor a ref the user deleted. An archived
    page under that key blocks it too — reviving an archived key would
    re-attach a stale identity — except when that archived page was merged
    INTO ``ref``: then it is this entity's own former key and reclaiming it is
    exactly right. In that case the archived copy's rel path is returned so
    the rename can move it aside (``<slug>_archived.md``) in the same commit.
    """
    type_, slug = _split(ref)
    new_slug = validate_new_slug(new_slug)
    if new_slug == slug:
        return None
    new_ref = f"{type_}:{new_slug}"
    memory = Path(workspace) / "memory"
    if (memory / "entities" / type_ / f"{new_slug}.md").exists():
        raise EntityRenameError(f"{new_ref} already exists")
    try:
        from durin.memory.deletion import is_deleted
        deleted = is_deleted(workspace, new_ref)
    except ImportError:  # pragma: no cover
        deleted = False
    if deleted:
        raise EntityRenameError(f"{new_ref} was deleted by the user; pick another key")
    arch = memory / "archive" / "entities" / type_ / f"{new_slug}.md"
    if not arch.exists():
        return None
    into = _merged_into(memory, arch)
    if into == ref:
        return arch.relative_to(memory).as_posix()
    raise EntityRenameError(
        f"{new_ref} is the key of an archived page"
        + (f" merged into {into}" if into else "") + "; pick another key")


def _merged_into(memory: Path, arch: Path) -> str | None:
    """The live ref an archived page ended up in, following ``archived_into``
    through pages that were themselves merged later (older merges did not
    redirect those pointers). None for a deleted page or a broken chain."""
    seen: set[Path] = set()
    into: str | None = None
    while arch.exists() and arch not in seen:
        seen.add(arch)
        page = EntityPage.from_file(arch)
        into = (page.extra.get("archived_into") or page.extra.get("absorbed_into")) if page else None
        if not into or ":" not in into:
            return None
        t, _, s = into.partition(":")
        if (memory / "entities" / t / f"{s}.md").exists():
            return into
        arch = memory / "archive" / "entities" / t / f"{s}.md"
    return into


def _free_archive_rel(workspace: Path, rel: str) -> str:
    memory = Path(workspace) / "memory"
    base = rel[:-3] + "_archived"
    cand, n = f"{base}.md", 2
    while (memory / cand).exists():
        cand, n = f"{base}_{n}.md", n + 1
    return cand


_STALE_RETRIES = 3


def rename_entity(
    workspace: Path,
    ref: str,
    new_slug: str,
    *,
    new_name: str | None = None,
    reason: str = "",
    author: bytes = b"durin-memory <memory@durin.local>",
    vector_index: object | None = None,
    exclude_aliases: set[str] | None = None,
) -> RenameResult:
    """Move entity ``ref`` to ``<type>:<new_slug>`` (optionally renaming its
    display name) and redirect every reference, in one commit.

    The new key must be free (see :func:`check_new_key`). The old slug and old
    name become aliases — except those in ``exclude_aliases`` (lowercase), which
    a pair resolution has just taken off this page on purpose. Side stores
    keyed by ref (merge tombstones, the Bandeja, the verdict cache) follow the
    new key; the alias index and the search indexes are refreshed best-effort
    (markdown is the source of truth). The rewrite is computed from the files
    as read and committed only if none of them changed meanwhile; otherwise it
    is recomputed (a few times) so a concurrent write is never overwritten."""
    from durin.memory.memory_writer import StaleContentError, write_files_cas

    type_, slug = _split(ref)
    new_slug = validate_new_slug(new_slug)
    new_ref = f"{type_}:{new_slug}"
    memory = Path(workspace) / "memory"
    old_path = memory / "entities" / type_ / f"{slug}.md"
    new_rel = f"entities/{type_}/{new_slug}.md"
    old_rel = f"entities/{type_}/{slug}.md"
    excluded = {a.lower() for a in (exclude_aliases or ())}

    for attempt in range(_STALE_RETRIES):
        if not old_path.exists():
            raise EntityRenameError(f"entity page missing: {ref}")
        raw = old_path.read_bytes()
        page = EntityPage.from_text(raw.decode("utf-8"))
        if page is None:
            raise EntityRenameError(f"could not parse entity page: {ref}")
        name_changes = bool(new_name and new_name.strip() and new_name.strip() != page.name)
        if new_slug == slug and not name_changes:
            return RenameResult(old_ref=ref, new_ref=ref, sha=None)
        reclaimed_archive = check_new_key(workspace, ref, new_slug)

        old_name = page.name
        aliases = list(page.aliases or [])
        keep = []
        if new_slug != slug:
            keep.append(slug)
        if name_changes:
            page.name = new_name.strip()
            keep.append(old_name)
        lowered = {a.lower() for a in aliases} | {page.name.lower()}
        for a in keep:
            if a and a.lower() not in lowered and a.lower() not in excluded:
                aliases.append(a)
                lowered.add(a.lower())
        page.aliases = aliases

        expect: dict[str, bytes | None] = {old_rel: raw}
        changes: dict[str, bytes | None] = {}
        rewrites: dict[str, bytes] = {}
        if new_slug != slug:
            expect[new_rel] = None
            rewrites = collect_ref_rewrites(workspace, ref, new_ref, skip={old_rel},
                                            originals=expect)
            changes.update(rewrites)
            changes[old_rel] = None
        if reclaimed_archive:
            # The archived duplicate that used to hold this key moves aside; its
            # pointer (already redirected above when it named the old key) stays.
            moved = changes.pop(reclaimed_archive, None)
            if moved is None:
                moved = (memory / reclaimed_archive).read_bytes()
            changes[reclaimed_archive] = None
            changes[_free_archive_rel(workspace, reclaimed_archive)] = moved
        changes[new_rel] = page.to_markdown().encode("utf-8")

        subject = (f"Rename {ref} to {new_ref}" if new_slug != slug
                   else f"Rename {ref} display name")
        msg = [subject, ""]
        if name_changes:
            msg.append(f"Display name: {old_name!r} -> {page.name!r}.")
        if rewrites:
            msg.append(f"Redirected {len(rewrites)} referencing file(s).")
        msg += ["", f"Renamed: {ref}", f"To: {new_ref}", f"Reason: {reason or 'rename'}"]
        try:
            sha = write_files_cas(workspace, changes, message="\n".join(msg),
                                  author=author, expect=expect)
            break
        except StaleContentError:
            if attempt == _STALE_RETRIES - 1:
                raise
            logger.info("rename %s: a file changed while renaming; recomputing", ref)

    entries: list[str] = []
    if new_slug != slug:
        entries = redirect_entry_refs(workspace, ref, new_ref)
        try:
            from durin.memory.refine_dream import rekey_ref_in_stores
            rekey_ref_in_stores(workspace, ref, new_ref)
        except Exception as exc:  # noqa: BLE001 — side stores are best-effort
            logger.warning("rename: side-store rekey failed for %s: %s", ref, exc)
    _refresh_indexes(workspace, old_ref=ref, new_ref=new_ref, page=page,
                     new_path=memory / new_rel, old_path=old_path,
                     vector_index=vector_index)
    return RenameResult(old_ref=ref, new_ref=new_ref, sha=sha,
                        rewritten=sorted(set(rewrites) | set(entries)))


def _refresh_indexes(workspace: Path, *, old_ref: str, new_ref: str,
                     page: EntityPage, new_path: Path, old_path: Path,
                     vector_index: object | None) -> None:
    """Keep the derived indexes current after a key change. Best-effort:
    every index here is rebuildable from the markdown."""
    try:
        from durin.memory.aliases_cache import get_shared_alias_index
        idx = get_shared_alias_index(Path(workspace) / "memory")
        if old_ref != new_ref:
            idx.remove(old_ref)
        idx.refresh_for(page, slug=new_ref.split(":", 1)[1])
    except Exception as exc:  # noqa: BLE001
        logger.warning("rename: alias index refresh failed: %s", exc)
    if vector_index is not None:
        try:
            if old_ref != new_ref:
                vector_index.delete_by_id(old_ref)
            vector_index.upsert_entity_page(
                entity_ref=new_ref, name=page.name, aliases=list(page.aliases),
                body=page.body, path=new_path, attributes=dict(page.attributes),
                relations=list(page.relations))
        except Exception as exc:  # noqa: BLE001
            logger.warning("rename: vector index refresh failed: %s", exc)
    try:
        from durin.memory.indexer import reindex_one_file
        if old_ref != new_ref:
            reindex_one_file(workspace, old_path, trigger="rename")
        reindex_one_file(workspace, new_path, trigger="rename")
    except Exception as exc:  # noqa: BLE001
        logger.warning("rename: FTS refresh failed: %s", exc)
