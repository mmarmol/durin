"""dulwich plumbing helpers for the optimistic memory writer.

Read/write git OBJECTS directly (Blob/Tree/Commit) without touching the
working tree, so concurrent writers never race on files (design §2.5). The
ref is moved by the caller via ``refs.set_if_equals`` (CAS).

Verified against dulwich 0.25.2: ``refs.follow(b"HEAD")`` returns
``(refnames, sha)``; ``tree[name]`` returns ``(mode, sha)``; ``tree.items()``
yields ``TreeEntry(path, mode, sha)``; ``Tree.add(name, mode, hexsha)``.
"""
from __future__ import annotations

import time
from pathlib import Path

from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

__all__ = [
    "default_ref",
    "head_sha",
    "read_blob_at_head",
    "build_commit_with_file",
]

_FILE_MODE = 0o100644
_DIR_MODE = 0o040000


def default_ref(repo: Repo) -> bytes:
    """The branch ref HEAD points at (``refs/heads/master`` or ``…/main``)."""
    refnames, _ = repo.refs.follow(b"HEAD")
    return refnames[-1]


def head_sha(root: Path) -> bytes | None:
    """Current commit sha of the memory repo, or None if there are no commits."""
    repo = Repo(str(root))
    try:
        ref = default_ref(repo)
        try:
            return repo.refs[ref]
        except KeyError:
            return None
    finally:
        repo.close()


def read_blob_at_head(root: Path, rel_path: str) -> bytes | None:
    """Return the bytes of ``rel_path`` at HEAD, or None if absent."""
    repo = Repo(str(root))
    try:
        ref = default_ref(repo)
        try:
            sha = repo.refs[ref]
        except KeyError:
            return None
        return _read_blob(repo, sha, rel_path)
    finally:
        repo.close()


def _read_blob(repo: Repo, commit_sha: bytes, rel_path: str) -> bytes | None:
    commit = repo.object_store[commit_sha]
    cur = repo.object_store[commit.tree]            # Tree
    parts = rel_path.encode().split(b"/")
    for i, part in enumerate(parts):
        names = {e.path for e in cur.items()}
        if part not in names:
            return None
        _mode, sha = cur[part]
        obj = repo.object_store[sha]
        if i == len(parts) - 1:
            return obj.data if isinstance(obj, Blob) else None
        cur = obj                                   # descend into subtree
    return None


def build_commit_with_file(
    root: Path,
    base_commit: bytes | None,
    rel_path: str,
    content: bytes,
    *,
    author: bytes,
    message: bytes,
) -> bytes:
    """Build (in the object store) a commit = ``base_commit`` with ``rel_path``
    set to ``content``. Returns the new commit sha. Does NOT move any ref or
    touch the working tree. Rebuilds tree objects copy-on-write up the path.
    """
    repo = Repo(str(root))
    try:
        store = repo.object_store
        blob = Blob.from_string(content)
        store.add_object(blob)
        parts = rel_path.encode().split(b"/")
        base_tree = store[store[base_commit].tree] if base_commit else Tree()
        new_root = _set_path(store, base_tree, parts, blob.id)
        store.add_object(new_root)
        commit = Commit()
        commit.tree = new_root.id
        commit.parents = [base_commit] if base_commit else []
        commit.author = commit.committer = author
        commit.author_time = commit.commit_time = int(time.time())
        commit.author_timezone = commit.commit_timezone = 0
        commit.encoding = b"utf-8"
        commit.message = message
        store.add_object(commit)
        return commit.id
    finally:
        repo.close()


def _set_path(store, tree: Tree, parts: list[bytes], blob_id: bytes) -> Tree:
    """Return a NEW Tree with ``parts`` → ``blob_id`` set, rebuilding subtrees.

    ``Tree.add`` is dict-backed (overwrites by name), so re-adding ``head``
    replaces the stale directory entry.
    """
    new = Tree()
    names = {e.path for e in tree.items()}
    for e in tree.items():
        new.add(e.path, e.mode, e.sha)
    if len(parts) == 1:
        new.add(parts[0], _FILE_MODE, blob_id)      # overwrites if exists
    else:
        head, rest = parts[0], parts[1:]
        sub = store[tree[head][1]] if head in names else Tree()
        new_sub = _set_path(store, sub, rest, blob_id)
        store.add_object(new_sub)
        new.add(head, _DIR_MODE, new_sub.id)         # overwrites old dir entry
    return new
