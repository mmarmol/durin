"""The single entity-write path: optimistic, multi-writer, via dulwich CAS.

A write = read page@HEAD → apply FieldPatches with precedence → build a commit
via plumbing (no working-tree mutation) → ``refs.set_if_equals`` CAS → retry on
conflict. The working tree is fast-forwarded to HEAD afterward so Obsidian and
other readers see the latest (design §2.5).

All first-class writers (agent upsert, dream extract/refine, dashboard) call
``write_entity``. The page-level ``author_scope`` (durin/memory/provenance.py)
is bridged to the field-level author when a patch leaves it None.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dulwich import porcelain
from dulwich.file import FileLocked
from dulwich.repo import Repo

from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch, apply_field_patch
from durin.memory.git_plumbing import (
    build_commit_with_file,
    default_ref,
    head_sha,
    read_blob_at_head,
)
from durin.memory.provenance import current_author

__all__ = ["WriteResult", "write_entity"]

_MAX_RETRIES = 30
# Bridge: page-level author (2 values) → field-level author (3 values).
# `dream` is passed explicitly by the dream paths; there is no page-level
# equivalent, by design (§2.4).
_PAGE_TO_FIELD = {"agent_created": "agent", "user_authored": "user"}


@dataclass
class WriteResult:
    ref: str
    committed: bool
    retries: int


def _rel_path(ref: str) -> str:
    type_, _, slug = ref.partition(":")
    return f"entities/{type_}/{slug}.md"


def _resolve_author(author: str | None) -> str:
    if author is not None:
        return author
    return _PAGE_TO_FIELD[current_author()]  # raises MissingAuthorScopeError if unset


def _ensure_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        porcelain.init(str(root))


def write_entity(
    workspace: Path,
    ref: str,
    patches: list[FieldPatch],
    *,
    create: bool = False,
    name: str | None = None,
) -> WriteResult:
    """Optimistically write ``patches`` into entity ``ref``'s page.

    ``name`` sets the display name (authored, not precedence-arbitrated).
    """
    root = Path(workspace) / "memory"
    _ensure_repo(root)
    rel = _rel_path(ref)
    type_, _, slug = ref.partition(":")

    # Resolve field-level author once (the ambient scope is stable per call).
    for patch in patches:
        patch.author = _resolve_author(patch.author)

    for attempt in range(_MAX_RETRIES):
        base = head_sha(root)
        raw = read_blob_at_head(root, rel)
        if raw is None:
            if not create:
                raise FileNotFoundError(
                    f"entity {ref} not found (pass create=True to author it)"
                )
            page = EntityPage(
                type=type_, name=slug, created_at=datetime.now(timezone.utc)
            )
        else:
            page = EntityPage.from_text(raw.decode("utf-8")) or EntityPage(
                type=type_, name=slug
            )

        # §2.4: default is agent-managed. memory_writer IS the agent/dream
        # write path, so every page it writes is agent_created. The user opts
        # to manage a page (page-level user_authored) via a SEPARATE path
        # (direct edit / dashboard), which the refine dream then leaves alone.
        # (Guarding memory_writer from writing an already-user_authored page is
        # the human-edit phase; here the writes are agent/dream by definition.)
        page.author = "agent_created"

        changed = False
        if name is not None and page.name != name:
            page.name = name
            changed = True
        for p in patches:
            changed = apply_field_patch(page, p) or changed
        if not changed and raw is not None:
            return WriteResult(ref, committed=False, retries=attempt)  # no-op

        page.updated_at = datetime.now(timezone.utc)
        content = page.to_markdown().encode("utf-8")
        new_commit = build_commit_with_file(
            root, base, rel, content,
            author=b"durin-memory <memory@durin.local>",
            message=f"upsert {ref}".encode("utf-8"),
        )
        repo = Repo(str(root))
        try:
            ok = repo.refs.set_if_equals(default_ref(repo), base, new_commit)
        except FileLocked:
            # dulwich's loose-ref CAS uses an O_EXCL lock file. Under
            # cross-writer contention it raises instead of returning False —
            # same recovery: back off and retry the whole read-apply-CAS.
            ok = False
        finally:
            repo.close()
        if ok:
            _fast_forward_working_tree(root)
            return WriteResult(ref, committed=True, retries=attempt)
        # CAS failed (mismatch or lock): HEAD may have moved → backoff + retry.
        time.sleep(random.uniform(0.0, 0.005) * (attempt + 1))

    raise RuntimeError(
        f"write_entity({ref}) exceeded {_MAX_RETRIES} CAS retries (high contention)"
    )


def _fast_forward_working_tree(root: Path) -> None:
    """Reset the working tree to HEAD so readers see the committed state.

    MVP: hard reset to HEAD. Safe in Phase 1 because every write goes through
    this module (no concurrent human working-tree edit). Resilient to the
    index/ref lock under concurrent writers (best-effort: a later ff converges
    the tree to HEAD anyway). The "don't ff over a dirty working tree" guard
    (design §2.5, finding 2D-1) lands with the human-edit work.
    # TODO(human-edit): skip ff when the working tree is dirty.
    """
    for i in range(10):
        try:
            porcelain.reset(str(root), "hard")
            return
        except (FileLocked, OSError):
            time.sleep(random.uniform(0.0, 0.005) * (i + 1))
    # Best-effort: give up silently; the canonical state is the commit (HEAD).
