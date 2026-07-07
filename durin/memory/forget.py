"""Forget a single memory entry: archive it + drop its index rows.

Single source of truth shared by the ``durin memory forget`` CLI and the
agent's ``memory_forget`` tool, so both keep the FTS + vector indices
consistent. A raw ``rm`` of an entry's ``.md`` (the only path the agent
had before ``memory_forget`` existed) leaves orphan index rows the
auto-repair can't reconstruct — this helper is the sanctioned way to
remove an entry without that breakage.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Classes a user/agent may forget. ``entities`` are excluded — they have
# their own absorb/revert lifecycle. ``pending`` is the intake buffer and
# is not user-visible.
FORGETTABLE_CLASSES: tuple[str, ...] = (
    "episodic", "stable", "corpus", "session_summary",
)


class ForgetError(Exception):
    """Raised when an entry can't be forgotten (bad uri, missing, refused)."""


def parse_memory_uri(uri: str) -> tuple[str, str]:
    """Split ``memory/<class>/<id>`` into ``(class_name, entry_id)``.

    Tolerates a leading ``./`` or trailing ``.md`` (callers paste either
    form). Raises :class:`ForgetError` on any other shape.
    """
    cleaned = uri.strip().lstrip("./")
    if cleaned.endswith(".md"):
        cleaned = cleaned[:-3]
    parts = cleaned.split("/")
    if len(parts) != 3 or parts[0] != "memory":
        raise ForgetError(f"expected 'memory/<class>/<id>', got: {uri!r}")
    return parts[1], parts[2]


def forget_entry(
    workspace: Path,
    uri: str,
    *,
    reason: str = "user_forget",
) -> Path:
    """Archive the entry at ``uri`` and remove its vector + FTS index rows.

    Returns the archive destination path
    (``memory/archive/<class>/<id>.md``).

    Raises :class:`ForgetError` for an invalid uri, an ``entities/`` uri
    (own lifecycle), an unsupported class, or a missing entry. Index
    cleanup is best-effort: the markdown move is the authoritative action,
    and a stale index row is reconciled by the next reindex / health-check
    self-heal — so a cleanup failure is logged, never raised.
    """
    workspace = Path(workspace)
    class_name, entry_id = parse_memory_uri(uri)

    if class_name == "entities":
        raise ForgetError(
            "refusing to forget entity pages; entities have their own "
            "lifecycle (use 'durin memory absorb' / 'durin memory revert')"
        )
    if class_name not in FORGETTABLE_CLASSES:
        raise ForgetError(
            f"unsupported class {class_name!r}; "
            f"supported: {', '.join(FORGETTABLE_CLASSES)}"
        )

    entry_path = workspace / "memory" / class_name / f"{entry_id}.md"
    if not entry_path.is_file():
        raise ForgetError(f"entry not found: {entry_path}")

    from durin.memory.archive import archive_episodic, archive_generic_entry

    if class_name == "episodic":
        dest = archive_episodic(
            workspace=workspace,
            episodic_path=entry_path,
            into_uri="",
            reason=reason,
        )
    else:
        dest = archive_generic_entry(
            workspace=workspace,
            entry_path=entry_path,
            reason=reason,
        )

    _drop_index_rows(workspace, uri=uri, entry_id=entry_id, entry_path=entry_path)
    return dest


def _drop_index_rows(
    workspace: Path,
    *,
    uri: str,
    entry_id: str,
    entry_path: Path,
) -> None:
    """Best-effort removal of the entry's vector + FTS rows. Never raises."""
    # Vector: model-free batched delete by id (no embedding model load).
    try:
        from durin.memory.vector_index import delete_ids

        delete_ids(workspace, [entry_id])
    except Exception as exc:  # noqa: BLE001
        logger.warning("forget: vector cleanup skipped for %s: %s", uri, exc)

    # FTS: delete the row by its exact uri (we hold the canonical
    # `memory/<class>/<id>` form, so no path→uri re-derivation needed).
    try:
        from durin.memory.fts_index import FTSIndex

        with FTSIndex.open(workspace) as idx:
            idx.delete_by_uri(uri)
    except Exception as exc:  # noqa: BLE001
        logger.warning("forget: FTS cleanup skipped for %s: %s", uri, exc)


def forget_reference(
    workspace: Path,
    ref: str,
    *,
    reason: str = "user_forget",
) -> Path | None:
    """Archive an ingested Library document and drop its index rows.

    The Library counterpart to :func:`forget_entry`. An ingested document is a
    ``reference:<slug>`` (its own archive + tombstone lifecycle, distinct from
    the ``memory/<class>/<id>`` entries above), so it is NOT a
    ``FORGETTABLE_CLASSES`` uri and does not flow through ``forget_entry``.

    Archives ``memory/references/<slug>.md`` (+ its ``.chunks.jsonl`` sidecar)
    and tombstones the ref via :func:`durin.memory.deletion.delete_reference`,
    then removes both index units the ingest wrote: the whole-doc FTS row
    (keyed ``reference:<slug>``) and every per-chunk vector row (keyed
    ``reference:<slug>#<idx>``). The tombstone only stops the dream from
    re-distilling — it does NOT filter search — so without this index cleanup
    the archived document keeps surfacing in ``memory_search`` and drilling it
    404s.

    Returns the archive destination, or ``None`` if no such reference exists.
    Index cleanup is best-effort (logged, never raised); the archive move +
    tombstone are the authoritative actions.
    """
    workspace = Path(workspace)
    slug = ref.split(":", 1)[1] if ":" in ref else ref
    ref = f"reference:{slug}"

    # Capture the chunk count BEFORE archiving moves the sidecar away: the
    # vector rows are keyed <ref>#<idx> for idx in range(chunk_count), and
    # the count is authoritative in the .chunks.jsonl the ingest wrote.
    from durin.memory.reference import reference_chunks

    chunk_count = len(reference_chunks(workspace, ref))

    from durin.memory.deletion import delete_reference

    dest = delete_reference(workspace, ref, reason=reason)
    if dest is None:
        return None

    _drop_reference_index_rows(workspace, ref=ref, chunk_count=chunk_count)
    return dest


def _drop_reference_index_rows(
    workspace: Path, *, ref: str, chunk_count: int,
) -> None:
    """Best-effort removal of a reference's FTS + vector rows. Never raises."""
    # FTS: the whole reference doc is a single row keyed by the ref uri.
    try:
        from durin.memory.fts_index import FTSIndex

        with FTSIndex.open(workspace) as idx:
            idx.delete_by_uri(ref)
    except Exception as exc:  # noqa: BLE001
        logger.warning("forget: FTS cleanup skipped for %s: %s", ref, exc)

    # Vector: one row per chunk, keyed <ref>#<idx>. Model-free id delete.
    if chunk_count > 0:
        try:
            from durin.memory.vector_index import delete_ids

            delete_ids(workspace, [f"{ref}#{i}" for i in range(chunk_count)])
        except Exception as exc:  # noqa: BLE001
            logger.warning("forget: vector cleanup skipped for %s: %s", ref, exc)
