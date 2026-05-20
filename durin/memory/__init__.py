"""Memory subsystem. See docs/08_memory_phase2_proposal.md §0c for design."""

from durin.memory.paths import (
    MEMORY_CLASSES,
    dream_dir,
    ingested_dir,
    ingested_entry_dir,
    memory_class_dir,
    memory_dir,
)
from durin.memory.provenance import Author, author_scope, current_author
from durin.memory.schema import MemoryEntry
from durin.memory.storage import FrontmatterError, load_entry, save_entry, split_frontmatter

__all__ = [
    "Author",
    "FrontmatterError",
    "MEMORY_CLASSES",
    "MemoryEntry",
    "author_scope",
    "current_author",
    "dream_dir",
    "ingested_dir",
    "ingested_entry_dir",
    "load_entry",
    "memory_class_dir",
    "memory_dir",
    "save_entry",
    "split_frontmatter",
]
