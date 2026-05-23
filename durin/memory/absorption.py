"""Entity absorption: merge two entity pages into one canonical.

Phase 5 per ``docs/19_implementation_plan.md`` §7. When the dream
detects that two pages refer to the same identity (aliases overlap,
identifier overlap, or LLM judgment), one is absorbed into the other:

- Canonical: receives merged aliases + identifiers + body section.
- Absorbed: moved to ``<canonical_slug>/archive/<absorbed_slug>.md``
  with frontmatter ``absorbed_into: ../../<canonical_slug>.md`` for
  traceability.
- Alias index drops the absorbed entity_ref (and any aliases unique
  to it become aliases of the canonical via the merged frontmatter).
- Vector index drops the absorbed entity row.

Designed to be safe to re-run: if the absorbed file is already
archived, ``absorb()`` is a no-op and returns the prior commit SHA
when reconstructible (else None).

This is **structural** absorption, not the temporal lifecycle from
doc 18 §10 R6 — no entries are dropped, just remapped. The whole
archive subfolder remains git-tracked, navigable, and accessible via
``durin memory expand``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_page import EntityPage
from durin.utils.git_repo import GitRepo, GitRepoError, NothingToCommitError

__all__ = [
    "AbsorptionError",
    "EntityAbsorption",
    "MergeCandidate",
]

logger = logging.getLogger(__name__)


class AbsorptionError(Exception):
    """Raised when absorption can't proceed (missing files, conflicting state)."""


@dataclass
class MergeCandidate:
    """A pair of entities that might be the same identity.

    ``shared_aliases`` is the list of alias strings that resolve to both
    refs in the current alias_index — the strongest determinist signal.
    """

    refs: tuple[str, str]
    shared_aliases: list[str] = field(default_factory=list)


class EntityAbsorption:
    """Coordinates merge detection + absorption + cleanup across indexes."""

    def __init__(
        self,
        workspace: Path,
        *,
        alias_index: AliasIndex | None = None,
        git_repo: GitRepo | None = None,
        vector_index: object | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.memory_root = self.workspace / "memory"
        self.entities_root = self.memory_root / "entities"
        self._alias_index = alias_index
        self._git_repo = git_repo
        self._vector_index = vector_index

    # ------------------------------------------------------------------
    # detect
    # ------------------------------------------------------------------

    def find_candidates(self) -> list[MergeCandidate]:
        """Return pairs of entities that share at least one alias.

        v1 signal: alias_index keys that point to >1 entity_ref. Each
        such key contributes a candidate per pair of refs. Ordering:
        candidates with more shared aliases come first (stronger signal).
        """
        idx = self._get_alias_index()
        # Group: pair (sorted) → list of shared alias strings.
        pairs: dict[tuple[str, str], list[str]] = {}
        for alias, refs in [(k, idx.lookup(k)) for k in idx.keys()]:
            if len(refs) < 2:
                continue
            for i in range(len(refs)):
                for j in range(i + 1, len(refs)):
                    a, b = sorted([refs[i], refs[j]])
                    pairs.setdefault((a, b), []).append(alias)

        out = [
            MergeCandidate(refs=pair, shared_aliases=sorted(set(aliases)))
            for pair, aliases in pairs.items()
        ]
        out.sort(key=lambda c: (-len(c.shared_aliases), c.refs))
        return out

    # ------------------------------------------------------------------
    # absorb
    # ------------------------------------------------------------------

    def absorb(
        self,
        canonical: str,
        absorbed: str,
        *,
        reason: str = "",
    ) -> str | None:
        """Merge ``absorbed`` into ``canonical``. Returns commit SHA or None.

        Steps:
        1. Load both pages from disk.
        2. Merge aliases + identifiers + body into canonical.
        3. Write updated canonical.
        4. Move absorbed file to ``<canonical_slug>/archive/<absorbed_slug>.md``.
        5. Stamp absorbed file's frontmatter with ``absorbed_into`` /
           ``absorbed_at`` / ``absorbed_reason``.
        6. Commit both changes in one git operation.
        7. Refresh alias_index (canonical) and remove absorbed entity_ref.
        8. Remove absorbed from vector_index (if provided).

        Idempotent: if absorbed file already lives in archive, return None.
        """
        canonical_type, canonical_slug = _split_ref(canonical)
        absorbed_type, absorbed_slug = _split_ref(absorbed)

        canonical_path = self.entities_root / canonical_type / f"{canonical_slug}.md"
        absorbed_path = self.entities_root / absorbed_type / f"{absorbed_slug}.md"

        if not canonical_path.exists():
            raise AbsorptionError(f"canonical page missing: {canonical_path}")
        if not absorbed_path.exists():
            # Maybe already archived?
            archive_target = (
                self.entities_root / canonical_type / canonical_slug / "archive"
                / f"{absorbed_slug}.md"
            )
            if archive_target.exists():
                logger.info("absorb: %s already archived; no-op", absorbed)
                return None
            raise AbsorptionError(f"absorbed page missing: {absorbed_path}")

        canonical_page = EntityPage.from_file(canonical_path)
        absorbed_page = EntityPage.from_file(absorbed_path)
        if canonical_page is None or absorbed_page is None:
            raise AbsorptionError(
                f"could not parse one or both pages "
                f"(canonical_parsed={canonical_page is not None}, "
                f"absorbed_parsed={absorbed_page is not None})"
            )

        # 2-3: merged canonical
        merged = _merge_pages(canonical_page, absorbed_page, absorbed_ref=absorbed)
        canonical_path.write_text(merged.to_markdown(), encoding="utf-8")

        # 4: move absorbed to archive subfolder of canonical
        archive_dir = (
            self.entities_root / canonical_type / canonical_slug / "archive"
        )
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived_path = archive_dir / f"{absorbed_slug}.md"

        # 5: stamp absorbed file frontmatter, then move it
        # Compute the relative link from archived → canonical for the
        # ``absorbed_into`` field (humans can click it in obsidian etc).
        # archived_path lives at: entities/<type>/<canonical_slug>/archive/<absorbed_slug>.md
        # canonical_path:        entities/<type>/<canonical_slug>.md
        # so the link is ../../<canonical_slug>.md
        from datetime import datetime, timezone

        absorbed_page.extra["absorbed_into"] = f"../../{canonical_slug}.md"
        absorbed_page.extra["absorbed_at"] = datetime.now(timezone.utc).isoformat()
        if reason:
            absorbed_page.extra["absorbed_reason"] = reason
        archived_path.write_text(absorbed_page.to_markdown(), encoding="utf-8")
        # Remove the original location — the file's new home is in archive.
        absorbed_path.unlink()

        # 6: git commit (covering all 3 file ops: canonical updated,
        # absorbed deleted from old path, archived created)
        repo = self._get_git_repo()
        repo.init(
            gitignore_patterns=[
                "*.lance/", "vectors/",
                ".aliases.json", ".usage.json", ".usage/",
                ".dream.lock", ".locks/",
            ]
        )
        try:
            sha = repo.commit(
                subject=f"Absorb {absorbed} into {canonical}",
                body=(
                    f"Merged {absorbed} into {canonical}. "
                    f"Reason: {reason or '(unspecified)'}.\n"
                    f"Absorbed page moved to "
                    f"entities/{canonical_type}/{canonical_slug}/archive/"
                    f"{absorbed_slug}.md."
                ),
                trailers={
                    "Absorbed": absorbed,
                    "Into": canonical,
                    "Reason": reason or "alias overlap",
                },
                paths=None,  # all three changes (canonical, deletion, archive)
                author="durin-dream",
                author_email="dream@durin.local",
            )
        except NothingToCommitError:
            logger.info("absorb: nothing to commit")
            sha = None

        # 7: alias_index — refresh canonical, drop absorbed entity_ref
        idx = self._get_alias_index()
        idx.refresh_for(merged, slug=canonical_slug)
        idx.remove(absorbed)
        idx.save()

        # 8: vector_index — drop absorbed page row (archived pages are
        # de-indexed by design per doc 18 §3 + R6)
        if self._vector_index is not None:
            try:
                self._vector_index.delete_by_id(absorbed)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "absorb: vector index delete failed for %s: %s",
                    absorbed, exc,
                )

        return sha

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _get_alias_index(self) -> AliasIndex:
        if self._alias_index is None:
            self._alias_index = AliasIndex(self.memory_root)
            if not self._alias_index.load():
                self._alias_index.build()
        return self._alias_index

    def _get_git_repo(self) -> GitRepo:
        if self._git_repo is None:
            self._git_repo = GitRepo(
                self.memory_root,
                default_author="durin-dream",
                default_email="dream@durin.local",
            )
        return self._git_repo


# ---------------------------------------------------------------------------
# Page merge — deterministic v1
# ---------------------------------------------------------------------------


def _merge_pages(
    canonical: EntityPage,
    absorbed: EntityPage,
    *,
    absorbed_ref: str,
) -> EntityPage:
    """Union aliases + identifiers, append absorbed body to canonical.

    v1 deterministic. The dream LLM can do smarter content merges by
    invoking DreamConsolidator with both pages as context — that's an
    opt-in path the caller controls. This function always succeeds and
    preserves content.
    """
    # Aliases: union (canonical first, then unseen from absorbed),
    # including the absorbed page's display name as alias (so the
    # canonical can be found by the absorbed's name).
    seen_aliases: set[str] = set()
    merged_aliases: list[str] = []
    for a in list(canonical.aliases) + [absorbed.name] + list(absorbed.aliases):
        if a and a not in seen_aliases:
            seen_aliases.add(a)
            merged_aliases.append(a)

    # Identifiers and other emergent fields: union when possible.
    merged_extra: dict = dict(canonical.extra)
    for key, value in absorbed.extra.items():
        if key in {"absorbed_into", "absorbed_at", "absorbed_reason"}:
            # Don't propagate absorption markers from a previously-absorbed
            # ancestor into the canonical (would confuse the trail).
            continue
        existing = merged_extra.get(key)
        if existing is None:
            merged_extra[key] = value
        elif isinstance(existing, list) and isinstance(value, list):
            seen = set(existing)
            merged_extra[key] = existing + [v for v in value if v not in seen]
        elif isinstance(existing, dict) and isinstance(value, dict):
            combined = dict(existing)
            for sub_k, sub_v in value.items():
                if sub_k not in combined:
                    combined[sub_k] = sub_v
            merged_extra[key] = combined
        # Otherwise: keep canonical's value (don't overwrite scalars).

    # Body: append a clearly-labeled section.
    section_header = f"\n\n## Absorbed from {absorbed_ref}\n\n"
    canonical_body = (canonical.body or "").rstrip("\n")
    absorbed_body = (absorbed.body or "").strip()
    if absorbed_body:
        new_body = canonical_body + section_header + absorbed_body + "\n"
    else:
        new_body = canonical_body + "\n"

    return EntityPage(
        type=canonical.type,
        name=canonical.name,
        aliases=merged_aliases,
        body=new_body,
        dream_processed_through=canonical.dream_processed_through,
        created_at=canonical.created_at,
        updated_at=canonical.updated_at,
        extra=merged_extra,
    )


def _split_ref(entity_ref: str) -> tuple[str, str]:
    if ":" not in entity_ref:
        raise AbsorptionError(f"bad entity_ref {entity_ref!r}: expected '<type>:<slug>'")
    type_, slug = entity_ref.split(":", 1)
    return type_, slug
