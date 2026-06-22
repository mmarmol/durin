"""Generic local-only versioned storage for derived artifacts.

Wraps :mod:`dulwich` (pure-Python git) so any durin subsystem can keep a
git history of its content without depending on the system ``git``
binary. Used by memory (entity pages) and conceptually applicable to
skills, future X — any subsystem with versionable artifacts.

Design — see ``docs/internals/memory/05_dream_cold_path.md`` §4:

- ``memory/.git/`` (or ``skills/.git/`` etc.) — strictly **local**, no
  remote.  durin never configures sync; that's user opt-in outside our
  scope.
- The owning subsystem decides:
  - When to ``init()`` (idempotent — wizard, first write, whenever).
  - What ``author`` / ``author_email`` to use (e.g.
    ``durin-dream <dream@durin.local>`` for memory consolidations,
    ``durin-curator <curator@durin.local>`` for skill refinements).
  - What structured trailers to emit (``Sources:``,
    ``Entities-touched:``, ``Skill:``, etc.).
- Commit message convention: ``subject`` + blank line + ``body`` +
  blank line + ``trailers`` (RFC822-style ``Key: value`` lines).
  Trailers are parsed back on read so callers can query "what entities
  were touched in commit X" without re-parsing prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dulwich import porcelain
from dulwich.errors import NotGitRepository
from dulwich.repo import Repo

__all__ = [
    "CommitInfo",
    "GitRepo",
    "GitRepoError",
    "NothingToCommitError",
]


# Trailer keys per git convention: letter-start, letters/digits/hyphens.
# We accept ``Sources``, ``Entities-touched``, ``Cursor-before``, etc.
_TRAILER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s*(.*)$")


class GitRepoError(Exception):
    """Base error for GitRepo operations."""


class NothingToCommitError(GitRepoError):
    """Raised (or signaled via None return) when commit has no changes."""


@dataclass(frozen=True)
class CommitInfo:
    """Parsed commit metadata. ``trailers`` are pre-parsed from the body."""

    sha: str
    author: str
    author_email: str
    timestamp: datetime
    subject: str
    body: str
    trailers: dict[str, list[str]] = field(default_factory=dict)


def _split_message(message: str) -> tuple[str, str, dict[str, list[str]]]:
    """Split a commit message into (subject, body, trailers).

    The convention: the last paragraph (block separated from the rest
    by a blank line) is parsed as trailers iff *all* its lines match
    the trailer pattern. Otherwise we treat it as part of the body.

    Trailer values that contain commas are split into a list; values
    without commas yield a single-element list. Repeated keys merge.
    """
    text = message.strip("\n")
    if not text:
        return "", "", {}

    lines = text.split("\n")
    subject = lines[0]
    rest = lines[1:]

    # Drop the blank line right after the subject if present.
    while rest and rest[0].strip() == "":
        rest.pop(0)

    if not rest:
        return subject, "", {}

    # Locate the last blank line so we can examine the trailing block.
    last_blank = -1
    for idx in range(len(rest) - 1, -1, -1):
        if rest[idx].strip() == "":
            last_blank = idx
            break

    if last_blank == -1:
        # Single block: either all trailers, or all body.
        if all(_TRAILER_LINE.match(line) for line in rest if line.strip()):
            body = ""
            trailer_lines = rest
        else:
            body = "\n".join(rest).rstrip("\n")
            return subject, body, {}
    else:
        trailer_block = rest[last_blank + 1:]
        # All-trailers test: every non-blank line in the trailing block
        # must match the trailer pattern. Otherwise it's body prose.
        if trailer_block and all(
            _TRAILER_LINE.match(line) for line in trailer_block if line.strip()
        ):
            body = "\n".join(rest[:last_blank]).rstrip("\n")
            trailer_lines = trailer_block
        else:
            return subject, "\n".join(rest).rstrip("\n"), {}

    # Parse trailers — repeated keys merge values, comma-separated splits.
    trailers: dict[str, list[str]] = {}
    for line in trailer_lines:
        if not line.strip():
            continue
        m = _TRAILER_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        items = [v.strip() for v in value.split(",") if v.strip()] or [value]
        trailers.setdefault(key, []).extend(items)

    return subject, body, trailers


def _format_trailers(trailers: dict[str, str | list[str]] | None) -> str:
    """Render trailers as ``Key: value`` lines (comma-joined for lists)."""
    if not trailers:
        return ""
    lines = []
    for key, value in trailers.items():
        if isinstance(value, (list, tuple)):
            joined = ", ".join(str(v) for v in value if v is not None)
        else:
            joined = str(value) if value is not None else ""
        lines.append(f"{key}: {joined}")
    return "\n".join(lines)


def _compose_message(
    subject: str,
    body: str = "",
    trailers: dict[str, str | list[str]] | None = None,
) -> str:
    """Compose subject + body + trailers per git convention."""
    parts = [subject.strip()]
    body_stripped = body.strip("\n").rstrip()
    if body_stripped:
        parts.append("")  # blank separator
        parts.append(body_stripped)
    trailers_text = _format_trailers(trailers)
    if trailers_text:
        parts.append("")  # blank separator
        parts.append(trailers_text)
    return "\n".join(parts) + "\n"


class GitRepo:
    """Local-only versioned storage wrapper around dulwich."""

    def __init__(
        self,
        root: Path,
        *,
        default_author: str = "durin-system",
        default_email: str = "system@durin.local",
    ) -> None:
        self.root = Path(root)
        self.default_author = default_author
        self.default_email = default_email

    # ------------------------------------------------------------------
    # init
    # ------------------------------------------------------------------

    def init(self, *, gitignore_patterns: list[str] | None = None) -> bool:
        """Initialize the repo if not already. Return True if it was created.

        Idempotent: calling twice is safe.  Writes ``.gitignore`` from
        the provided patterns when the repo is freshly created (won't
        overwrite an existing one). Creates an initial empty commit so
        ``log()`` and ``status()`` work immediately.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            Repo(str(self.root))
            return False  # already initialized
        except NotGitRepository:
            pass

        porcelain.init(str(self.root))

        # Drop a .gitignore if patterns given and no file exists.
        # If init runs in a dir that already has content, the existing
        # files stay untracked — they'll be picked up by the next
        # explicit commit, not bundled into the init commit.
        if gitignore_patterns:
            gi = self.root / ".gitignore"
            if not gi.exists():
                gi.write_text("\n".join(gitignore_patterns) + "\n",
                              encoding="utf-8")

        # Initial commit. We stage ONLY the marker file we control
        # (.gitignore if just written; .gitkeep otherwise) — never
        # `git add -A` — so any pre-existing content in the directory
        # stays untracked until the subsystem explicitly commits it.
        marker_path: Path
        if (self.root / ".gitignore").exists() and gitignore_patterns:
            marker_path = self.root / ".gitignore"
        else:
            marker_path = self.root / ".gitkeep"
            marker_path.write_text("", encoding="utf-8")
        self.commit(
            subject="Initialize repository",
            paths=[marker_path],
            author=self.default_author,
            author_email=self.default_email,
        )
        return True

    def is_initialized(self) -> bool:
        try:
            Repo(str(self.root))
            return True
        except NotGitRepository:
            return False

    # ------------------------------------------------------------------
    # commit
    # ------------------------------------------------------------------

    def commit(
        self,
        *,
        subject: str,
        body: str = "",
        trailers: dict[str, str | list[str]] | None = None,
        paths: list[Path] | None = None,
        author: str | None = None,
        author_email: str | None = None,
    ) -> str | None:
        """Stage paths (or all changes) and commit. Return SHA or None.

        - ``paths=None``: ``git add -A`` equivalent (stages all changes).
        - ``paths=[...]``: stage only those paths.
        - Returns ``None`` and raises :class:`NothingToCommitError` if
          there are no changes to commit.
        - Author defaults come from the GitRepo instance; can override
          per-commit (e.g. when memory has multiple write origins).
        """
        repo = self._require_repo()
        author = author or self.default_author
        author_email = author_email or self.default_email
        actor_bytes = f"{author} <{author_email}>".encode("utf-8")

        # Stage changes
        if paths is None:
            # Add everything (porcelain.add accepts the repo and discovers).
            try:
                porcelain.add(str(self.root))
            except Exception as exc:  # noqa: BLE001
                raise GitRepoError(f"failed to stage all: {exc}") from exc
        else:
            try:
                porcelain.add(
                    str(self.root),
                    [str(Path(p).resolve()) for p in paths],
                )
            except Exception as exc:  # noqa: BLE001
                raise GitRepoError(f"failed to stage paths {paths}: {exc}") from exc

        # Detect: are there staged changes?
        if not self._has_staged_changes(repo):
            raise NothingToCommitError("no changes staged")

        message = _compose_message(subject, body, trailers)
        try:
            sha = porcelain.commit(
                str(self.root),
                message=message.encode("utf-8"),
                author=actor_bytes,
                committer=actor_bytes,
            )
        except Exception as exc:  # noqa: BLE001
            raise GitRepoError(f"commit failed: {exc}") from exc

        return sha.decode("ascii") if isinstance(sha, bytes) else str(sha)

    @staticmethod
    def _has_staged_changes(repo: Repo) -> bool:
        """True iff the index differs from HEAD (or HEAD doesn't exist yet)."""
        # If no HEAD yet, any staged file counts.
        try:
            head = repo[repo.head()]
        except KeyError:
            # No HEAD: initial commit. Any indexed entry counts.
            return bool(list(repo.open_index()))
        head_tree_id = head.tree
        index_tree_id = repo.open_index().commit(repo.object_store)
        return index_tree_id != head_tree_id

    # ------------------------------------------------------------------
    # log / show / diff
    # ------------------------------------------------------------------

    def log(
        self,
        path: Path | None = None,
        *,
        max_count: int = 50,
    ) -> list[CommitInfo]:
        """Return commits touching *path* (or all commits if None).

        Newest-first. Trailers are parsed eagerly so callers can filter
        ``Entities-touched``, ``Sources`` etc. without re-reading the
        message.
        """
        repo = self._require_repo()
        walker = repo.get_walker(max_entries=max_count)

        target_path: bytes | None = None
        if path is not None:
            try:
                rel = Path(path).resolve().relative_to(self.root.resolve())
            except ValueError:
                # path is outside the repo root
                return []
            target_path = str(rel).encode("utf-8")

        out: list[CommitInfo] = []
        for entry in walker:
            commit = entry.commit
            if target_path is not None and not self._commit_touches(commit, repo, target_path):
                continue
            sha = commit.id.decode("ascii")
            message = commit.message.decode("utf-8", errors="replace")
            subject, body, trailers = _split_message(message)
            author_text = commit.author.decode("utf-8", errors="replace")
            author, email = _split_author(author_text)
            ts = datetime.fromtimestamp(commit.commit_time, tz=timezone.utc)
            out.append(
                CommitInfo(
                    sha=sha,
                    author=author,
                    author_email=email,
                    timestamp=ts,
                    subject=subject,
                    body=body,
                    trailers=trailers,
                )
            )
        return out

    @staticmethod
    def _commit_touches(commit, repo: Repo, path: bytes) -> bool:
        """True iff *commit* changed *path* compared to its first parent."""
        try:
            tree = repo[commit.tree]
        except KeyError:
            return False
        # Walk the tree to see if path exists.  Naive but sufficient for
        # phase 1 — for large repos we'd switch to tree-diff against parent.
        return _path_in_tree(repo, tree, path.split(b"/"))

    def show(self, sha: str, path: Path | None = None) -> str:
        """Return file content at *sha* (or full message + diff if no path)."""
        repo = self._require_repo()
        try:
            commit = repo[sha.encode("ascii")]
        except (KeyError, TypeError, ValueError) as exc:
            raise GitRepoError(f"commit not found: {sha}") from exc

        if path is None:
            # Default: just the message. (Full patch is what `diff` gives.)
            return commit.message.decode("utf-8", errors="replace")

        rel = Path(path).resolve().relative_to(self.root.resolve())
        target = str(rel).encode("utf-8")
        tree = repo[commit.tree]
        blob = _resolve_blob(repo, tree, target.split(b"/"))
        if blob is None:
            raise GitRepoError(f"path {rel} not found at {sha[:8]}")
        return blob.data.decode("utf-8", errors="replace")

    def diff(
        self,
        from_sha: str,
        to_sha: str,
        path: Path | None = None,
    ) -> str:
        """Unified diff between two commits (newest semantics: to vs from).

        ``path``: optional scope, returns only changes to that file.
        """
        import io
        repo = self._require_repo()
        try:
            from_commit = repo[from_sha.encode("ascii")]
            to_commit = repo[to_sha.encode("ascii")]
        except (KeyError, TypeError, ValueError) as exc:
            raise GitRepoError(f"commit lookup failed: {exc}") from exc

        buf = io.BytesIO()
        porcelain.diff_tree(
            repo.path,
            from_commit.tree,
            to_commit.tree,
            outstream=buf,
        )
        text = buf.getvalue().decode("utf-8", errors="replace")
        if path is not None:
            rel = str(Path(path).resolve().relative_to(self.root.resolve()))
            # Filter the unified diff to only sections about that path.
            kept_sections = []
            current_section: list[str] = []
            include = False
            for line in text.split("\n"):
                if line.startswith("diff --git"):
                    if include and current_section:
                        kept_sections.append("\n".join(current_section))
                    current_section = [line]
                    include = rel in line
                else:
                    current_section.append(line)
            if include and current_section:
                kept_sections.append("\n".join(current_section))
            return "\n".join(kept_sections)
        return text

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, list[str]]:
        """Working tree state. Keys: 'staged', 'modified', 'untracked'."""
        repo = self._require_repo()
        s = porcelain.status(repo.path)
        # porcelain.status returns GitStatus(staged, unstaged, untracked).
        # Staged itself is a dict {'add': [...], 'modify': [...], 'delete': [...]}.
        staged: list[str] = []
        for files in s.staged.values():
            staged.extend(p.decode("utf-8") if isinstance(p, bytes) else str(p)
                          for p in files)
        modified = [p.decode("utf-8") if isinstance(p, bytes) else str(p)
                    for p in s.unstaged]
        untracked = [str(p) for p in s.untracked]
        return {
            "staged": staged,
            "modified": modified,
            "untracked": untracked,
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_repo(self) -> Repo:
        try:
            return Repo(str(self.root))
        except NotGitRepository as exc:
            raise GitRepoError(
                f"{self.root} is not a git repo; call init() first"
            ) from exc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _split_author(text: str) -> tuple[str, str]:
    """Split ``Name <email>`` into (name, email). Returns ('', text) on fail."""
    m = re.match(r"^(.*?)\s*<([^>]+)>\s*$", text)
    if not m:
        return text, ""
    return m.group(1), m.group(2)


def _path_in_tree(repo: Repo, tree, parts: list[bytes]) -> bool:
    """Recursive lookup: is the path present in this tree?"""
    if not parts:
        return False
    head, rest = parts[0], parts[1:]
    for entry in tree.items():
        if entry.path == head:
            if not rest:
                return True
            try:
                sub = repo[entry.sha]
            except KeyError:
                return False
            return _path_in_tree(repo, sub, rest)
    return False


def _resolve_blob(repo: Repo, tree, parts: list[bytes]):
    """Resolve a tree path to a blob object, or None if missing."""
    if not parts:
        return None
    head, rest = parts[0], parts[1:]
    for entry in tree.items():
        if entry.path == head:
            obj = repo[entry.sha]
            if not rest:
                return obj
            return _resolve_blob(repo, obj, rest)
    return None
