"""File-backed library of SOUL personality docs.

The ``default`` soul is the workspace ``SOUL.md`` (kept at the workspace root
for backward compatibility and existing git tracking); every other soul is a
file under ``workspace/souls/<slug>.md``.
"""
from __future__ import annotations

import re
from pathlib import Path

DEFAULT_SLUG = "default"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class SoulStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.souls_dir = self.workspace / "souls"

    def _path(self, slug: str) -> Path:
        if not _SLUG_RE.match(slug):
            raise ValueError(f"invalid soul slug: {slug!r}")
        if slug == DEFAULT_SLUG:
            return self.workspace / "SOUL.md"
        return self.souls_dir / f"{slug}.md"

    def exists(self, slug: str) -> bool:
        return self._path(slug).exists()

    def read(self, slug: str) -> str:
        path = self._path(slug)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write(self, slug: str, body: str) -> None:
        path = self._path(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def delete(self, slug: str) -> None:
        if slug == DEFAULT_SLUG:
            raise ValueError("cannot delete the default soul")
        path = self._path(slug)
        if path.exists():
            path.unlink()

    def list(self) -> list[str]:
        slugs: list[str] = []
        if (self.workspace / "SOUL.md").exists():
            slugs.append(DEFAULT_SLUG)
        if self.souls_dir.is_dir():
            slugs.extend(f.stem for f in self.souls_dir.glob("*.md"))
        return sorted(set(slugs))
