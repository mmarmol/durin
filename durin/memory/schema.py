"""Memory entry schema — frontmatter + body model.

Matches the frontmatter spec in `docs/08_memory_phase2_proposal.md`
§0c.5 (multi-resolution memory entries). The schema is a pydantic
model so writes validate at construction time and reads catch
malformed frontmatter early.

Resolution semantics:
- ``headline`` (~10 words) — pulled in bulk into the hot layer.
- ``summary`` (~50 words) — returned by ``memory_search(level="warm")``.
- ``body`` (~200-500 words) — returned by ``memory_search(level="cold")``
  or by ``memory_drill``. Lives outside the frontmatter (markdown after
  the closing ``---``).
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from durin.memory.provenance import Author

__all__ = ["MemoryEntry"]


class MemoryEntry(BaseModel):
    """One memory entry: frontmatter fields + markdown body."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    id: str
    headline: str
    summary: str = ""
    source_refs: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    author: Author = "user_authored"
    valid_from: Optional[date] = None
    body: str = ""
