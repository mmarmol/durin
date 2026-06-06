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
    build_commit_with_changes,
    build_commit_with_file,
    default_ref,
    head_sha,
    read_blob_at_head,
)
from durin.memory.provenance import current_author

__all__ = ["WriteResult", "write_entity", "write_files_cas"]

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


def _commit_dirty_as_user(root: Path) -> None:
    """Human-edit guard (design §2.5, N1): before a system write touches git,
    commit any uncommitted working-tree ``.md`` edits with author ``user`` — so
    the hard-reset ff (``_fast_forward_working_tree``) that follows can't clobber
    an in-progress hand edit (e.g. the user editing a page in Obsidian). No-op on
    a clean tree. Best-effort — a failure here must never block the system write.
    """
    if not (root / ".git").exists():
        return
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
    """A3 — per-entity relation cap (doc 01 §4.4), soft 50 / hard 200.

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
    _commit_dirty_as_user(root)  # N1: preserve any in-progress hand edit first
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
            _emit_relation_cap(ref, rel_before, rel_after)
            return WriteResult(ref, committed=True, retries=attempt)
        # CAS failed (mismatch or lock): HEAD may have moved → backoff + retry.
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
    _commit_dirty_as_user(root)  # N1: preserve any in-progress hand edit first
    msg = message.encode("utf-8") if isinstance(message, str) else message
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
            # dulwich `reset --hard` does not reliably remove working-tree files
            # that are absent from the target tree; enforce deletions explicitly
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
