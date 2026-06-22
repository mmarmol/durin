"""Memory subsystem."""

from durin.memory.consolidator_tags import parse_consolidator_response
from durin.memory.drill import DrillError, drill
from durin.memory.embedding import EmbeddingProvider, FastembedProvider
from durin.memory.hot_layer import HotLayer, read_hot_layer
from durin.memory.ingestion import IngestError, ingest_artifact
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
from durin.memory.search import Result as SearchResult
from durin.memory.search import search_memory
from durin.memory.session_md import (
    SessionMdError,
    regenerate_session_md,
    render_session_md,
)
from durin.memory.storage import FrontmatterError, load_entry, save_entry, split_frontmatter
from durin.memory.store import StoreError, store_memory
from durin.memory.vector_index import VectorIndex, vector_index_available

__all__ = [
    "Author",
    "DrillError",
    "EmbeddingProvider",
    "FastembedProvider",
    "VectorIndex",
    "FrontmatterError",
    "HotLayer",
    "IngestError",
    "MEMORY_CLASSES",
    "MemoryEntry",
    "SessionMdError",
    "StoreError",
    "author_scope",
    "current_author",
    "dream_dir",
    "drill",
    "ingest_artifact",
    "ingested_dir",
    "ingested_entry_dir",
    "load_entry",
    "memory_class_dir",
    "memory_dir",
    "parse_consolidator_response",
    "read_hot_layer",
    "regenerate_session_md",
    "render_session_md",
    "SearchResult",
    "save_entry",
    "search_memory",
    "split_frontmatter",
    "store_memory",
    "vector_index_available",
]
