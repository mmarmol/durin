"""Resolutions for a pair of entity pages the dedup pass could not simply merge.

Two pages that collide (a shared alias, or embedding-near) are not always
"the same" or "different". Often they are *related* — an edition and the game
it belongs to, a specific rule and the general one — or one of them carries an
alias that belongs to the other, or a key that says too little. A plain
"keep separate" leaves the collision in place: both pages keep answering to
the shared alias, and the next page with that alias raises it again.

A :class:`Resolution` says what to do with the pair:

- ``merge`` — fold one page into the other. ``survivor`` picks which key
  survives; the survivor may also get a clearer key (``renames``).
- ``disambiguate`` — keep both, but settle who owns each contested alias
  (``alias_moves``) and optionally give either page a clearer key.
- ``relate`` — ``disambiguate`` plus a typed edge between the two pages
  (``relation``: e.g. ``edition_of``, ``part_of``, ``specializes``).
- ``keep`` — keep both exactly as they are.

The judges propose one (see :func:`resolution_from_judge`), the dream applies
confident ones on its own, and the Bandeja lets the user accept, edit or
override the proposal. :func:`validate_resolution` is the single gate every
resolution passes before :func:`apply_resolution` touches memory.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from durin.memory.entity_page import EntityPage

__all__ = [
    "AliasMove",
    "ApplyResult",
    "RelationSpec",
    "RenameSpec",
    "Resolution",
    "PairKeptSeparateError",
    "PairPageMissingError",
    "ResolutionError",
    "RESOLUTION_KINDS",
    "apply_resolution",
    "effective_resolution",
    "resolution_from_judge",
    "validate_resolution",
]

logger = logging.getLogger(__name__)

RESOLUTION_KINDS = ("merge", "disambiguate", "relate", "keep")
_KEEP_ON_SPECIAL = ("both", "none")


class ResolutionError(ValueError):
    """The resolution does not fit the pair (unknown ref, bad slug, …)."""


class PairKeptSeparateError(ResolutionError):
    """An automatic merge of a pair the user kept separate — refused, and no
    fallback may perform it."""


class PairPageMissingError(ResolutionError):
    """A page of the pair no longer exists (already merged, renamed or
    deleted) — the pair is stale rather than the resolution wrong."""


@dataclass
class AliasMove:
    """Who keeps ``alias``: one of the two refs (removed from the other, added
    to it if missing), ``both`` (present on both — a legitimate homonym) or
    ``none`` (junk — e.g. OCR noise — removed from both pages)."""

    alias: str
    keep_on: str


@dataclass
class RelationSpec:
    from_ref: str
    type: str
    to_ref: str


@dataclass
class RenameSpec:
    slug: str | None = None
    name: str | None = None


@dataclass
class Resolution:
    kind: str
    survivor: str | None = None
    renames: dict[str, RenameSpec] = field(default_factory=dict)
    alias_moves: list[AliasMove] = field(default_factory=list)
    relation: RelationSpec | None = None
    confidence: int = 0
    reasoning: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["renames"] = {k: asdict(v) for k, v in self.renames.items()}
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Resolution":
        """Tolerant reader for stored proposals, judge JSON and API payloads.

        Accepts ``relation`` as ``{from, type, to}`` or ``{from_ref, type,
        to_ref}``, ``renames`` as ``{ref: {slug, name}}`` or ``{ref: slug}``,
        and ``alias_moves`` items as ``{alias, keep_on}`` or ``{alias, to}``.
        Shape errors raise :class:`ResolutionError`; meaning is checked by
        :func:`validate_resolution`."""
        if not isinstance(data, dict):
            raise ResolutionError("resolution must be an object")

        def _s(v: Any) -> str | None:
            # Scalars only: a judge that answers a list or an object where a
            # string belongs gets the field dropped, never a crash.
            if v is None or isinstance(v, (dict, list, tuple, set, bool)):
                return None
            text = str(v).strip()
            return text or None

        kind = (_s(data.get("kind")) or _s(data.get("action")) or "").lower()
        renames: dict[str, RenameSpec] = {}
        raw_ren = data.get("renames") or data.get("rename") or {}
        if isinstance(raw_ren, dict):
            items = list(raw_ren.items())
        elif isinstance(raw_ren, list):
            items = [(m.get("ref"), m) for m in raw_ren if isinstance(m, dict)]
        else:
            items = []
        for ref, spec in items:
            ref = _s(ref)
            if not ref:
                continue
            if isinstance(spec, dict):
                slug, name = _s(spec.get("slug")), _s(spec.get("name"))
            else:
                slug, name = _s(spec), None
            if slug or name:
                renames[ref] = RenameSpec(slug=slug, name=name)
        moves: list[AliasMove] = []
        raw_moves = data.get("alias_moves")
        for m in raw_moves if isinstance(raw_moves, list) else []:
            if not isinstance(m, dict):
                continue
            alias = _s(m.get("alias"))
            if not alias:
                continue
            keep_on = _s(m.get("keep_on")) or _s(m.get("to")) or _s(m.get("owner")) or ""
            moves.append(AliasMove(alias=alias, keep_on=keep_on))
        rel = data.get("relation")
        relation = None
        if isinstance(rel, dict) and rel:
            relation = RelationSpec(
                from_ref=_s(rel.get("from_ref")) or _s(rel.get("from")) or "",
                type=_s(rel.get("type")) or "",
                to_ref=_s(rel.get("to_ref")) or _s(rel.get("to")) or "",
            )
        try:
            confidence = int(data.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        return cls(
            kind=kind,
            survivor=_s(data.get("survivor")),
            renames=renames,
            alias_moves=moves,
            relation=relation,
            confidence=confidence,
            reasoning=_s(data.get("reasoning")) or "",
            source=_s(data.get("source")) or "",
        )

    def changes_anything(self) -> bool:
        """True when applying it would change memory beyond a tombstone."""
        if self.kind == "merge":
            return True
        return bool(
            self.relation is not None
            or self.alias_moves
            or any(r.slug or r.name for r in self.renames.values())
        )


@dataclass
class ApplyResult:
    kind: str
    refs: dict[str, str]  # original ref -> ref after the resolution (merged-away -> survivor)
    commits: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# From a judge verdict
# --------------------------------------------------------------------------

_VERDICT_TO_KIND = {"same": "merge", "related": "relate"}


def resolution_from_judge(
    verdict: str,
    proposal: dict[str, Any] | None,
    ref_a: str,
    ref_b: str,
    *,
    confidence: int = 0,
    reasoning: str = "",
    source: str = "",
) -> Resolution | None:
    """The resolution a judge's answer implies, or None when it implies none.

    The verdict decides the kind (``same`` → merge, ``related`` → relate,
    ``different`` → disambiguate when the proposal changes something, else
    keep); the optional proposal block fills in survivor, renames, alias moves
    and the relation. ``unclear`` yields a resolution only when the judge
    still proposed concrete operations — it is shown to the user, never
    applied automatically. A malformed proposal degrades to the bare verdict
    rather than failing the judgment."""
    try:
        res = Resolution.from_dict(proposal or {}) if proposal else Resolution(kind="")
    except ResolutionError:
        res = Resolution(kind="")
    kind = _VERDICT_TO_KIND.get(verdict)
    if kind is None:
        if verdict == "different":
            kind = "disambiguate"
        elif verdict == "unclear":
            # An unsure judge may still suggest edits for the user to look at,
            # never a merge: "not sure they are the same" is not a merge.
            kind = res.kind if res.kind in ("disambiguate", "relate", "keep") else ""
        else:
            return None
    if kind == "relate" and res.relation is None:
        kind = "disambiguate"
    res.kind = kind
    if kind == "merge" and not res.survivor:
        res.survivor = ref_a
    if kind == "disambiguate" and not res.changes_anything():
        res.kind = "keep"
    if not res.kind:
        return None
    res.confidence = confidence
    res.reasoning = reasoning
    res.source = source
    return res


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _alias_owners(alias: str, page_a: EntityPage | None,
                  page_b: EntityPage | None) -> tuple[bool, bool]:
    low = alias.strip().lower()

    def _has(page: EntityPage | None) -> bool:
        # A page answers to its display name as much as to its aliases.
        return bool(page) and (
            any(a.lower() == low for a in page.aliases or [])
            or (page.name or "").strip().lower() == low)

    return _has(page_a), _has(page_b)


def validate_resolution(
    res: Resolution,
    ref_a: str,
    ref_b: str,
    page_a: EntityPage | None,
    page_b: EntityPage | None,
) -> Resolution:
    """Check ``res`` against the pair and return a normalized copy.

    Raises :class:`ResolutionError` naming the first problem. Normalizes:
    relation type (the shared relation-label form), rename slugs, alias moves
    for aliases neither page carries (dropped — the judge may quote a name),
    ``keep_on`` spelled as a slug or a bare ``a``/``b``. The pages are only
    read for alias moves, so a merge validates without them."""
    from durin.memory.entity_rename import EntityRenameError, validate_new_slug
    from durin.memory.field_patch import normalize_relation_type

    pair = (ref_a, ref_b)
    if res.kind not in RESOLUTION_KINDS:
        raise ResolutionError(f"unknown resolution kind {res.kind!r}")
    out = Resolution(kind=res.kind, confidence=res.confidence,
                     reasoning=res.reasoning, source=res.source)

    def _ref(value: str | None, what: str) -> str:
        v = (value or "").strip()
        if v in pair:
            return v
        low = v.lower()
        if low in ("a", "ref_a", "page a"):
            return ref_a
        if low in ("b", "ref_b", "page b"):
            return ref_b
        for r in pair:  # a bare slug
            if low and r.split(":", 1)[1] == v:
                return r
        raise ResolutionError(f"{what} {value!r} is not one of {ref_a}, {ref_b}")

    if res.kind == "merge":
        out.survivor = _ref(res.survivor or ref_a, "survivor")
    for ref, spec in (res.renames or {}).items():
        target = _ref(ref, "rename target")
        if res.kind == "merge" and target != out.survivor:
            continue  # the merged-away page is archived; renaming it is moot
        slug = spec.slug.strip() if spec.slug else None
        if slug:
            try:
                slug = validate_new_slug(slug)
            except EntityRenameError as exc:
                raise ResolutionError(str(exc)) from exc
            if slug == target.split(":", 1)[1]:
                slug = None
        name = spec.name.strip() if spec.name else None
        if slug or name:
            out.renames[target] = RenameSpec(slug=slug, name=name)
    new_keys = [f"{r.split(':', 1)[0]}:{s.slug}" for r, s in out.renames.items() if s.slug]
    if len(set(new_keys)) != len(new_keys):
        raise ResolutionError("both pages cannot be renamed to the same key")
    for key in new_keys:
        if key in pair:
            raise ResolutionError(f"rename target {key} is the other page of the pair")

    if res.kind != "merge":
        seen: set[str] = set()
        for m in res.alias_moves or []:
            alias = (m.alias or "").strip()
            if not alias or alias.lower() in seen:
                continue
            on_a, on_b = _alias_owners(alias, page_a, page_b)
            if not (on_a or on_b):
                continue
            keep = (m.keep_on or "").strip()
            if keep.lower() in _KEEP_ON_SPECIAL:
                keep = keep.lower()
            else:
                keep = _ref(keep, f"owner of alias {alias!r}")
            seen.add(alias.lower())
            out.alias_moves.append(AliasMove(alias=alias, keep_on=keep))

    if res.kind == "relate":
        rel = res.relation
        if rel is None:
            raise ResolutionError("relate needs a relation")
        frm = _ref(rel.from_ref, "relation source")
        to = _ref(rel.to_ref, "relation target")
        if frm == to:
            raise ResolutionError("a relation needs two different pages")
        rtype = normalize_relation_type(rel.type)
        if not rtype:
            raise ResolutionError("relation type is empty")
        out.relation = RelationSpec(from_ref=frm, type=rtype, to_ref=to)
    return out


def effective_resolution(
    res: Resolution | None,
    ref_a: str,
    ref_b: str,
    page_a: EntityPage,
    page_b: EntityPage,
) -> Resolution | None:
    """``res`` validated against the pages and stripped of operations that
    would change nothing (an alias already where the move puts it, an edge the
    page already has). A proposal that does not validate degrades to the bare
    verdict (a merge keeps its default survivor, anything else becomes
    ``keep``), so one malformed field never turns a clear verdict into a
    failed auto-apply. None stays None."""
    if res is None:
        return None
    try:
        out = validate_resolution(res, ref_a, ref_b, page_a, page_b)
    except ResolutionError:
        bare = Resolution(kind=res.kind if res.kind == "merge" else "keep",
                          survivor=ref_a if res.kind == "merge" else None,
                          confidence=res.confidence, reasoning=res.reasoning,
                          source=res.source)
        return bare
    if out.kind == "merge":
        return out
    pages = {ref_a: page_a, ref_b: page_b}
    moves = []
    for m in out.alias_moves:
        if m.keep_on == "both":
            # From a judge "both" means "leave it"; copying one page's alias
            # onto the other would create the very collision being resolved.
            # (A user who wants that says so in the Bandeja editor.)
            continue
        on_a, on_b = _alias_owners(m.alias, page_a, page_b)
        now = "both" if on_a and on_b else (ref_a if on_a else ref_b)
        if m.keep_on != now:
            moves.append(m)
    out.alias_moves = moves
    if out.relation is not None:
        src = pages[out.relation.from_ref]
        if any(r.get("to") == out.relation.to_ref and r.get("type") == out.relation.type
               for r in src.relations or []):
            out.relation = None
    if out.kind == "relate" and out.relation is None:
        out.kind = "disambiguate"
    if out.kind == "disambiguate" and not out.changes_anything():
        out.kind = "keep"
    return out


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------

_AUTHORS = {
    "user": (b"user <user@durin.local>", "user"),
    "dream": (b"durin-dream <dream@durin.local>", "dream"),
}


def _page_path(workspace: Path, ref: str) -> Path:
    t, _, s = ref.partition(":")
    return Path(workspace) / "memory" / "entities" / t / f"{s}.md"


def _load(workspace: Path, ref: str) -> EntityPage:
    path = _page_path(workspace, ref)
    if not path.exists():
        raise PairPageMissingError(f"entity page missing: {ref}")
    page = EntityPage.from_file(path)
    if page is None:
        raise ResolutionError(f"entity page unparseable: {ref}")
    return page


def _set_aliases(page: EntityPage, alias: str, present: bool) -> bool:
    low = alias.lower()
    has = [a for a in page.aliases or [] if a.lower() == low]
    if present and not has:
        page.aliases = list(page.aliases or []) + [alias]
        return True
    if not present and has:
        page.aliases = [a for a in page.aliases or [] if a.lower() != low]
        return True
    return False


def apply_resolution(
    workspace: Path,
    res: Resolution,
    ref_a: str,
    ref_b: str,
    *,
    actor: str = "user",
    vector_index: object | None = None,
    allow_rename: bool = True,
) -> ApplyResult:
    """Apply a (validated or not) resolution to the pair.

    Order: the merge or the alias/relation edits first (one commit), then the
    renames (one commit each, every reference redirected — including the new
    relation, which is why it is written before). A resolution applied by the
    user tombstones the pair unless it merged it, so the dream never merges
    what the user decided to keep apart; the dream's own resolutions leave no
    tombstone (its verdict is remembered by the verdict cache instead).
    Removes the pair from the Bandeja. ``allow_rename=False`` drops the
    renames (the dream's ``auto_rename`` switch)."""
    from durin.memory.absorption import EntityAbsorption
    from durin.memory.entity_rename import rename_entity
    from durin.memory.refine_dream import add_tombstone, remove_flagged

    if actor not in _AUTHORS:
        raise ResolutionError(f"unknown actor {actor!r}")
    commit_author, field_author = _AUTHORS[actor]
    if res.kind == "merge":
        # The merge reads the pages itself (and reports a stale pair).
        page_a = page_b = None
    else:
        page_a, page_b = _load(workspace, ref_a), _load(workspace, ref_b)
    res = validate_resolution(res, ref_a, ref_b, page_a, page_b)
    if allow_rename:
        # Every new key is checked before anything is written, so a taken key
        # rejects the whole resolution instead of leaving it half applied.
        from durin.memory.entity_rename import EntityRenameError, check_new_key
        for ref, spec in res.renames.items():
            if spec.slug:
                try:
                    check_new_key(workspace, ref, spec.slug)
                except EntityRenameError as exc:
                    raise ResolutionError(str(exc)) from exc
    result = ApplyResult(kind=res.kind, refs={ref_a: ref_a, ref_b: ref_b})
    note = f"{actor} resolution ({res.source or 'bandeja'})"
    removed: dict[str, set[str]] = {}

    if res.kind == "merge":
        survivor = res.survivor or ref_a
        other = ref_b if survivor == ref_a else ref_a
        if actor == "dream":
            from durin.memory.refine_dream import is_tombstoned
            if is_tombstoned(workspace, ref_a, ref_b):
                # The user kept this pair apart; no automatic path may merge it.
                raise PairKeptSeparateError(f"{ref_a} / {ref_b} were kept separate by the user")
        sha = EntityAbsorption(workspace=workspace, vector_index=vector_index).absorb(
            survivor, other, reason="manual_review" if actor == "user" else "refine",
            judge_reasoning=res.reasoning or None,
            judge_confidence=res.confidence or None,
        )
        if sha:
            result.commits.append(sha)
        elif not _page_path(workspace, other).exists():
            # absorb() is a no-op when the page is already archived: the pair
            # is stale (merged elsewhere since it was flagged), not merged now.
            raise PairPageMissingError(f"entity page missing: {other}")
        result.refs = {ref_a: survivor, ref_b: survivor}
    else:
        removed = _apply_edits(workspace, res, ref_a, ref_b, note=note,
                               commit_author=commit_author, field_author=field_author,
                               commits=result.commits)
    if allow_rename:
        for ref, spec in res.renames.items():
            current = result.refs.get(ref, ref)
            new_slug = spec.slug or current.split(":", 1)[1]
            rr = rename_entity(workspace, current, new_slug, new_name=spec.name,
                               reason=note, author=commit_author,
                               vector_index=vector_index,
                               exclude_aliases=removed.get(ref))
            if rr.sha:
                result.commits.append(rr.sha)
            for k, v in list(result.refs.items()):
                if v == current:
                    result.refs[k] = rr.new_ref

    if actor == "user" and res.kind != "merge":
        final_a, final_b = result.refs[ref_a], result.refs[ref_b]
        add_tombstone(workspace, final_a, final_b)
    remove_flagged(workspace, ref_a, ref_b)
    remove_flagged(workspace, result.refs[ref_a], result.refs[ref_b])
    return result


def _apply_edits(workspace: Path, res: Resolution, ref_a: str, ref_b: str, *,
                 note: str, commit_author: bytes, field_author: str,
                 commits: list[str]) -> dict[str, set[str]]:
    """Commit the alias moves and the relation in one commit, derived from
    the pages as read and retried if either changed meanwhile. Returns, per
    ref, the aliases (lowercase) the resolution took off that page — a rename
    that follows must not put them back as "old slug / old name" aliases."""
    from durin.memory.field_patch import FieldPatch, apply_field_patch
    from durin.memory.memory_writer import StaleContentError, write_files_cas

    removed: dict[str, set[str]] = {ref_a: set(), ref_b: set()}
    for attempt in range(3):
        raws = {r: _page_path(workspace, r).read_bytes() for r in (ref_a, ref_b)}
        pages = {r: EntityPage.from_text(raw.decode("utf-8")) for r, raw in raws.items()}
        if any(p is None for p in pages.values()):
            raise ResolutionError("entity page unparseable")
        removed = {ref_a: set(), ref_b: set()}
        changed: set[str] = set()
        for m in res.alias_moves:
            for ref, page in pages.items():
                keep = m.keep_on == "both" or m.keep_on == ref
                if _set_aliases(page, m.alias, present=keep):
                    changed.add(ref)
                if not keep:
                    # Also when it was this page's display name rather than an
                    # alias: a rename must not bring it back as the "old name".
                    removed[ref].add(m.alias.lower())
        if res.relation is not None:
            src = pages[res.relation.from_ref]
            patch = FieldPatch(
                kind="relation", source_ref=note, at=datetime.now(timezone.utc),
                author=field_author,
                value={"to": res.relation.to_ref, "type": res.relation.type},
            )
            if apply_field_patch(src, patch):
                changed.add(res.relation.from_ref)
        if not changed:
            return removed
        changes: dict[str, bytes | None] = {}
        expect: dict[str, bytes | None] = {}
        for ref in sorted(changed):
            t, _, s_ = ref.partition(":")
            rel = f"entities/{t}/{s_}.md"
            changes[rel] = pages[ref].to_markdown().encode("utf-8")
            expect[rel] = raws[ref]
        msg = [f"Resolve {ref_a} / {ref_b}: {res.kind}", "", f"Applied by: {note}."]
        if res.reasoning:
            msg += ["", "Reasoning:", res.reasoning.strip()]
        msg += ["", f"Pair: {ref_a} {ref_b}", f"Resolution: {res.kind}"]
        if res.confidence:
            msg.append(f"Judge-Confidence: {int(res.confidence)}")
        try:
            sha = write_files_cas(workspace, changes, message="\n".join(msg),
                                  author=commit_author, expect=expect)
        except StaleContentError:
            if attempt == 2:
                raise
            continue
        if sha:
            commits.append(sha)
        _refresh_aliases(workspace, {r: pages[r] for r in changed})
        return removed
    return removed


def _refresh_aliases(workspace: Path, pages: dict[str, EntityPage]) -> None:
    try:
        from durin.memory.aliases_cache import get_shared_alias_index
        idx = get_shared_alias_index(Path(workspace) / "memory")
        for ref, page in pages.items():
            idx.refresh_for(page, slug=ref.split(":", 1)[1])
    except Exception as exc:  # noqa: BLE001 — the index is rebuildable
        logger.warning("pair resolution: alias index refresh failed: %s", exc)
