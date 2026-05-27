"""Archive episodic entries consumed by a successful Dream apply.

Per `docs/memory/05_dream_cold_path.md` §7 + `docs/memory/01_data_and_entities.md` §3.6:

After ``apply_dream_output`` writes a new entity-page version, the
episodic entries cited in the patch's ``provenance`` fields are
**moved** to ``memory/archive/episodic/``. Stable, corpus, and
pending entries are NOT auto-archived (only the agent or the user
can decide to archive those — they carry intent the Dream consumer
doesn't have).

This step is intentionally separated from
:func:`durin.memory.dream_apply.apply_dream_output`:

- It runs only on successful apply (the caller decides), so a
  rolled-back patch never archives.
- The vector-index drop is best-effort and isolated here so a
  LanceDB error doesn't poison the apply path.
- Cleaner unit boundary — applier mutates one page; this module
  mutates many entries.

The caller (DreamConsolidator / runner) invokes this after a
successful apply, then advances the cursor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from durin.memory.archive import archive_episodic
from durin.memory.dream_patch_parser import ParsedDreamOutput

__all__ = ["ArchiveConsumedResult", "archive_consumed_episodic"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArchiveConsumedResult:
    """Outcome of the archive step.

    ``archived``
        Provenance refs (as they appeared in the patch) that were
        successfully moved. Ordered by first appearance in the patch
        for stable telemetry.
    ``errors``
        Human-readable error strings for non-fatal issues (missing
        source file, vector-index delete crash). Failure to archive
        one ref does NOT abort the rest.
    """

    archived: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def archive_consumed_episodic(
    *,
    workspace: Path,
    entity_ref: str,
    parsed: ParsedDreamOutput,
    vector_index: Optional[Any] = None,
) -> ArchiveConsumedResult:
    """Archive every unique ``episodic/<id>.md`` ref in *parsed*'s ops.

    Returns immediately with empty result when *parsed* carries no
    patch ops.

    The ``vector_index`` argument is optional — when supplied, the
    function calls ``vector_index.delete_by_id(<entry_id>)`` for each
    archived ref. Any exception there is recorded in ``errors`` and
    swallowed; the on-disk archive is the durable source of truth.
    """
    archived: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()

    for op in parsed.patch_ops:
        provenance = op.get("provenance")
        if not isinstance(provenance, str) or not provenance.strip():
            continue
        if provenance in seen:
            continue
        seen.add(provenance)

        if not provenance.startswith("episodic/"):
            # Stable / corpus / pending: per §5.3-§5.5 doc 01, do not
            # auto-archive. Silently skip.
            continue

        # Resolve to absolute path under the workspace.
        rel = provenance
        src = Path(workspace) / "memory" / rel
        if not src.is_file():
            errors.append(f"source not found: {provenance}")
            continue

        try:
            archive_episodic(
                workspace=Path(workspace),
                episodic_path=src,
                into_uri=entity_ref,
                reason="dream_consolidated",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"archive failed for {provenance}: {exc}")
            continue

        archived.append(provenance)

        # Vector index delete is best-effort.
        if vector_index is not None:
            entry_id = src.stem
            try:
                vector_index.delete_by_id(entry_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"vector_index delete for {provenance} failed: {exc}"
                )
                logger.warning(
                    "dream_archive_consumed: vector delete %s failed: %s",
                    entry_id, exc,
                )

    return ArchiveConsumedResult(archived=archived, errors=errors)
