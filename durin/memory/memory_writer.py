"""The single entity-write path: optimistic, multi-writer, via dulwich CAS.

A write = read page@HEAD → apply FieldPatches with precedence → build a commit
via plumbing (no working-tree mutation) → ``refs.set_if_equals`` CAS → retry on
conflict. The working tree is fast-forwarded to HEAD afterward so Obsidian and
other readers see the latest.

All first-class writers (agent upsert, dream extract/refine, dashboard) call
``write_entity``. The page-level ``author_scope`` (durin/memory/provenance.py)
is bridged to the field-level author when a patch leaves it None.
"""
from __future__ import annotations

import random
import threading
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
    build_commit_with_changes,
    build_commit_with_file,
    default_ref,
    head_sha,
    read_blob_at_head,
)
from durin.memory.provenance import current_author
from durin.utils.file_lock import cross_process_lock

# Imported lazily inside _refresh_alias_index to avoid a circular import:
# aliases_cache → AliasIndex → entity_page → (no memory_writer dep), so the
# direct import is safe; the lazy form is used for symmetry with other
# best-effort helpers in this module.


def _refresh_alias_index(memory_root: Path, page: EntityPage, slug: str) -> None:
    """Incrementally refresh the shared AliasIndex after a successful write.

    Guards: only refreshes if the index has already been built this process
    (don't force-build on every write — the search pipeline builds lazily on
    first use). Best-effort: a refresh failure must never fail the write.

    Guards AliasIndex staleness (hazard #17).
    """
    try:
        from durin.memory.aliases_cache import _cache, get_shared_alias_index

        # Guard: skip if the shared index has not been built yet in this process.
        if _cache.get(memory_root) is None:
            return
        get_shared_alias_index(memory_root).refresh_for(page, slug)
    except Exception:  # noqa: BLE001 — best-effort; never block the write
        pass

__all__ = ["WriteResult", "git_worktree_lock_path", "write_entity", "write_files_cas"]


def git_worktree_lock_path(memory_git_root: Path) -> Path:
    """Return the canonical cross-process lock target for git working-tree mutations.

    All three sites that must not race a dulwich reset --hard agree on this path:
    - memory_writer._commit_dirty_as_user / _fast_forward_working_tree (mutations)
    - indexer.reindex_one_file (prune branch, sub-hazard B)
    - vector_index.prune_orphan_rows (prune branch, sub-hazard B)

    The lock FILE is ``<memory_git_root>/.git-worktree.lock``; the TARGET
    (the arg to cross_process_lock) is ``<memory_git_root>/.git-worktree``.
    Kept here so callers don't hardcode the path in three places.
    """
    return Path(memory_git_root) / ".git-worktree"

_MAX_RETRIES = 30
# Bridge: page-level author (2 values) → field-level author (3 values).
# `dream` is passed explicitly by the dream paths; there is no page-level
# equivalent, by design.
_PAGE_TO_FIELD = {"agent_created": "agent", "user_authored": "user"}

# In-process per-repo write lock (in-process ref-CAS thread race, NOT audit
# #9/#18). The bare loose-ref CAS (`refs.set_if_equals` under a `GitFile`
# O_EXCL `.lock`) is robust on its own across both processes and threads.
# The in-process loss comes from the concurrent, UNLOCKED
# `_fast_forward_working_tree` (`reset --hard`): this lock serializes that
# reset cross-process via `.git-worktree`, but in-process it was
# not serialized against a peer thread's read-apply-CAS. Mid-reset the ref is
# transiently stale/absent (`head_sha()` can return None), so a peer reads a
# stale `base`, commits on it, and its CAS lands — orphaning a concurrent commit
# and losing that write WITHOUT any error or CAS-retry. We close that gap by
# serializing the whole read-apply-CAS-reset section of same-process writers to a
# given repo root.
#
# Lock ordering (verified): this RLock is the OUTERMOST memory lock — it is
# acquired only at the top of `write_entity` / `write_files_cas`, strictly
# BEFORE the `.git-worktree` `cross_process_lock` taken inside
# `_commit_dirty_as_user` / `_fast_forward_working_tree` (which remains the inner
# lock). No path takes `.git-worktree` and then this RLock (the only
# `.git-worktree` holders are the indexer/vector_index prune paths, which never
# call back into the writers), so there is no opposite-order edge and no
# deadlock. RLock (reentrant) so the two writers can nest on one thread without
# self-deadlock. It is in-process only, so it cannot deadlock cross-process.
_root_write_locks: dict[str, threading.RLock] = {}
_root_write_locks_guard = threading.Lock()


def _root_write_lock(root: Path) -> threading.RLock:
    """Return the process-wide RLock for memory repo ``root`` (keyed by the
    resolved path), creating it on first use under a small guard lock."""
    key = str(Path(root).resolve())
    with _root_write_locks_guard:
        lock = _root_write_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _root_write_locks[key] = lock
        return lock


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


def _commit_dirty_as_user(root: Path) -> None:
    """Human-edit guard: before a system write touches git,
    commit any uncommitted working-tree ``.md`` edits with author ``user`` — so
    the hard-reset ff (``_fast_forward_working_tree``) that follows can't clobber
    an in-progress hand edit (e.g. the user editing a page in Obsidian). No-op on
    a clean tree. Best-effort — a failure here must never block the system write.

    Serialized under the git-worktree cross-process lock: this lock is the sole
    guard for working-tree / .git/index mutations and is always acquired AFTER
    any dulwich-internal ref CAS lock.  It is never held while a session or
    config lock is acquired, so there is no opposite-order acquisition.
    """
    if not (root / ".git").exists():
        return
    # Sub-hazard A: concurrent commit + reset can corrupt .git/index (dulwich
    # _transition_to_file is non-atomic).  This lock is the OUTERMOST memory
    # lock — always acquired AFTER any dulwich-internal ref CAS lock, never
    # while a session/config lock is held.
    # The prune paths (indexer.reindex_one_file, vector_index.prune_orphan_rows)
    # also acquire this lock (sub-hazard B recheck) THEN their FTS/Lance locks
    # (inner).  No path takes an FTS/Lance lock and THEN this lock, so lock
    # ordering is strict: git-worktree > FTS/Lance.
    with cross_process_lock(git_worktree_lock_path(root)):
        try:
            status = porcelain.status(str(root))
        except Exception:  # noqa: BLE001 — never block a write on the guard
            return
        dirty: list[str] = []
        for group in (status.unstaged, status.untracked):
            for item in group:
                rel = item.decode("utf-8") if isinstance(item, bytes) else item
                if not rel.endswith(".md"):
                    continue
                parts = Path(rel).parts
                if parts and parts[0] in ("archive", "pending"):
                    continue
                dirty.append(rel)
        if not dirty:
            return
        try:
            porcelain.add(str(root), [str(root / d) for d in dirty])
            porcelain.commit(
                str(root),
                message=b"manual edit (human-edit guard)",
                author=b"user <user@durin.local>",
                committer=b"user <user@durin.local>",
            )
        except Exception:  # noqa: BLE001
            return


def _emit_relation_cap(ref: str, before: int, after: int) -> None:
    """Per-entity relation cap, soft 50 / hard 200.

    Alert-only "de momento": emit telemetry + log when a write crosses the soft
    or hard cap, but NEVER block the write or drop a relation (no data loss).
    Best-effort — telemetry must never break a write.
    """
    if after <= before:
        return
    try:
        from durin.memory.entity_relation_cap import (
            HARD_RELATION_CAP,
            SOFT_RELATION_CAP,
            check_relation_cap,
        )
        decision = check_relation_cap(
            entity_ref=ref, current_count=before, adding=after - before)
        if decision.action == "ok":
            return
        payload = {"entity_ref": ref, "current_count": before, "new_count": after}
        from durin.agent.tools._telemetry import emit_tool_event
        # Literal event names (the catalog scanner reads the string at the call).
        if decision.action == "warn":
            emit_tool_event("memory.entity_relation_cap_warned", payload)
        else:
            emit_tool_event("memory.entity_relation_cap_rejected", payload)
        from loguru import logger
        logger.warning(
            "relation cap {} for {}: {} -> {} relations (soft={} hard={}; "
            "alert-only, not enforced)",
            decision.action, ref, before, after,
            SOFT_RELATION_CAP, HARD_RELATION_CAP,
        )
    except Exception:  # pragma: no cover — never break a write
        pass


def _compose_entity_commit_message(
    ref: str,
    *,
    is_new: bool,
    name_changed: bool,
    patches: list[FieldPatch],
) -> bytes:
    """Build an informative commit message for an entity write.

    The subject names the entity and the kinds of field touched; the body
    lists each change one per line; trailers carry the provenance
    ``source_ref``(s) and the field-author scope. This is what makes the
    ``git log`` / dashboard history readable instead of a wall of bare
    ``upsert <ref>`` lines (the absorb path keeps its own richer format).
    """
    n_rel = sum(1 for p in patches if p.kind == "relation")
    n_attr = sum(1 for p in patches if p.kind == "attribute")
    n_alias = sum(1 for p in patches if p.kind == "alias")
    n_src = sum(1 for p in patches if p.kind == "derived_from")
    has_body = any(p.kind in ("body_append", "body_replace") for p in patches)

    summary: list[str] = []
    if name_changed:
        summary.append("name")
    if has_body:
        summary.append("body")
    if n_rel:
        summary.append(f"+{n_rel} relation")
    if n_attr:
        summary.append(f"{n_attr} attribute")
    if n_alias:
        summary.append(f"{n_alias} alias")
    if n_src:
        summary.append(f"+{n_src} source")

    subject = f"{'create' if is_new else 'update'} {ref}"
    if summary:
        subject += ": " + ", ".join(summary)

    body_lines: list[str] = []
    for p in patches:
        if p.kind == "body_append":
            body_lines.append("body append")
        elif p.kind == "body_replace":
            body_lines.append("body replace")
        elif p.kind == "relation":
            v = p.value or {}
            body_lines.append(f"relation → {v.get('to', '?')} ({v.get('type', '?')})")
        elif p.kind == "attribute":
            body_lines.append(f"attribute {p.key}")
        elif p.kind == "alias":
            body_lines.append(f"alias {p.value}")
        elif p.kind == "derived_from":
            body_lines.append(f"derived_from {p.value}")

    # dict.fromkeys preserves first-seen order while de-duplicating.
    sources = list(dict.fromkeys(p.source_ref for p in patches if p.source_ref))
    authors = list(dict.fromkeys(p.author for p in patches if p.author))

    lines = [subject]
    if body_lines:
        lines += ["", *body_lines]
    trailers: list[str] = []
    if sources:
        trailers.append(f"Source: {', '.join(sources)}")
    if authors:
        trailers.append(f"Author: {', '.join(authors)}")
    if trailers:
        lines += ["", *trailers]
    return ("\n".join(lines) + "\n").encode("utf-8")


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

    # Serialize same-process writers to this repo around the whole
    # read-apply-CAS-reset (in-process thread race — the concurrent unlocked
    # _fast_forward_working_tree reset transiently corrupts the ref, feeding a
    # stale base to a peer thread's CAS). Outer lock; the inner `.git-worktree`
    # lock is taken below inside _commit_dirty_as_user / _fast_forward_working_tree.
    with _root_write_lock(root):
        _commit_dirty_as_user(root)  # preserve any in-progress hand edit first
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

            # Default is agent-managed. memory_writer IS the agent/dream
            # write path, so every page it writes is agent_created. The user opts
            # to manage a page (page-level user_authored) via a SEPARATE path
            # (direct edit / dashboard), which the refine dream then leaves alone.
            # (Guarding memory_writer from writing an already-user_authored page
            # is the human-edit phase; here writes are agent/dream by definition.)
            page.author = "agent_created"

            is_new = raw is None
            changed = False
            name_changed = name is not None and page.name != name
            if name_changed:
                page.name = name
                changed = True
            rel_before = len(page.relations)
            for p in patches:
                changed = apply_field_patch(page, p) or changed
            rel_after = len(page.relations)
            if not changed and raw is not None:
                return WriteResult(ref, committed=False, retries=attempt)  # no-op

            page.updated_at = datetime.now(timezone.utc)
            content = page.to_markdown().encode("utf-8")
            new_commit = build_commit_with_file(
                root, base, rel, content,
                author=b"durin-memory <memory@durin.local>",
                message=_compose_entity_commit_message(
                    ref, is_new=is_new, name_changed=name_changed, patches=patches,
                ),
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
                _emit_relation_cap(ref, rel_before, rel_after)
                _refresh_alias_index(root, page, slug)
                return WriteResult(ref, committed=True, retries=attempt)
            # CAS failed (mismatch or lock): HEAD may have moved → backoff+retry.
            time.sleep(random.uniform(0.0, 0.005) * (attempt + 1))

    raise RuntimeError(
        f"write_entity({ref}) exceeded {_MAX_RETRIES} CAS retries (high contention)"
    )


def write_files_cas(
    workspace: Path,
    changes: dict[str, bytes | None],
    *,
    message: str,
    author: bytes = b"durin-memory <memory@durin.local>",
) -> str | None:
    """Commit MULTIPLE file changes atomically via plumbing + CAS, then ff.

    ``changes`` maps rel_path (under ``memory/``) → bytes (write) or None
    (delete). This is the multi-file analogue of ``write_entity`` — the entity
    merge uses it so the refine commits like memory_writer (no porcelain
    working-tree staging, resilient to ref-lock contention). Returns the new
    commit sha (hex) on commit; None if ``changes`` is empty.
    """
    if not changes:
        return None
    root = Path(workspace) / "memory"
    _ensure_repo(root)
    msg = message.encode("utf-8") if isinstance(message, str) else message
    # Serialize same-process writers to this repo around the whole
    # read-apply-CAS-reset (in-process thread race — see write_entity and the
    # registry note above). Outer lock; `.git-worktree` is taken inside (inner).
    with _root_write_lock(root):
        _commit_dirty_as_user(root)  # preserve any in-progress hand edit first
        for attempt in range(_MAX_RETRIES):
            base = head_sha(root)
            new_commit = build_commit_with_changes(
                root, base, changes, author=author, message=msg)
            repo = Repo(str(root))
            try:
                ok = repo.refs.set_if_equals(default_ref(repo), base, new_commit)
            except FileLocked:
                ok = False
            finally:
                repo.close()
            if ok:
                _fast_forward_working_tree(root)
                # dulwich `reset --hard` does not reliably remove working-tree
                # files absent from the target tree; enforce deletions explicitly
                # so the working tree matches the commit.
                for rel_path, content in changes.items():
                    if content is None:
                        fpath = root / rel_path
                        try:
                            fpath.unlink()
                        except FileNotFoundError:
                            pass
                        except OSError:  # pragma: no cover
                            pass
                return new_commit.decode("ascii")
            time.sleep(random.uniform(0.0, 0.005) * (attempt + 1))
    raise RuntimeError(
        f"write_files_cas exceeded {_MAX_RETRIES} CAS retries (high contention)"
    )


def _fast_forward_working_tree(root: Path) -> None:
    """Reset the working tree to HEAD so readers see the committed state.

    MVP: hard reset to HEAD. Resilient to the index/ref lock under concurrent
    writers (best-effort: a later ff converges the tree to HEAD anyway).

    Sub-hazard A: serialized under the git-worktree cross-process lock so a
    concurrent commit and reset cannot interleave and corrupt .git/index.
    Sub-hazard B (the transient absent-file window): dulwich reset --hard uses
    unlink-then-recreate (_transition_to_file), opening a window during which
    files are transiently absent.  Prune paths (reindex_one_file,
    prune_orphan_rows) close B by acquiring THIS SAME lock before acting on an
    absent-file observation and re-checking is_file() after acquiring it.  If
    the file is present on re-check, the reset completed and the prune is
    skipped.

    cross_process_lock is reentrant per-thread: the common caller sequence
    _commit_dirty_as_user → (CAS) → _fast_forward_working_tree re-enters safely.
    """
    with cross_process_lock(git_worktree_lock_path(root)):
        for i in range(10):
            try:
                porcelain.reset(str(root), "hard")
                return
            except (FileLocked, OSError):
                time.sleep(random.uniform(0.0, 0.005) * (i + 1))
    # Best-effort: give up silently; the canonical state is the commit (HEAD).
