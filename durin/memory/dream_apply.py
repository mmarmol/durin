"""Apply a parsed v2 Dream output to an entity page on disk.

Per `docs/memory/05_dream_cold_path.md` §6:

  1. Validate every patch op (allowed root, provenance present,
     known op type).
  2. Copy current entity page to `<path>.md.bak`.
  3. Apply ops to a dict view of the page (attributes / relations /
     aliases) via :mod:`jsonpatch`.
  4. Record provenance into `page.provenance[…]` per op.
  5. Append body delta to the body if non-empty.
  6. Re-render markdown, write atomically (temp + rename).
  7. Re-parse the written file; if it doesn't satisfy
     ``EntityPage.from_file`` (round-trip failure), restore from
     `.md.bak` and surface `ROUND_TRIP`.
  8. On any earlier failure, restore from `.md.bak` and surface a
     typed `DreamApplyFailureKind`.
  9. On success, delete the `.md.bak`.

The cursor advance (`dream_processed_through`) is NOT done here —
that lives in the runner so the G2 invariant (doc 05 §6.1) stays in
one place.
"""

from __future__ import annotations

import enum
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonpatch
import jsonpointer

from durin.memory.dream_patch_parser import ParsedDreamOutput
from durin.memory.entity_page import EntityPage, EntityPageError

__all__ = [
    "DreamApplyError",
    "DreamApplyFailureKind",
    "DreamApplyResult",
    "apply_dream_output",
]

logger = logging.getLogger(__name__)


class DreamApplyError(Exception):
    """Pre-condition failure that the caller cannot recover from
    (e.g. malformed entity_ref, missing canonical page). Raised before
    any disk write — there is nothing to roll back."""


class DreamApplyFailureKind(str, enum.Enum):
    """Failure categories — used both for telemetry (doc 07 §6.4) and
    for the quarantine counter (doc 05 §12)."""

    VALIDATION = "validation"
    PATCH_RUNTIME = "patch_runtime"
    ROUND_TRIP = "round_trip"
    IO = "io"


@dataclass(frozen=True)
class DreamApplyResult:
    """Outcome of one apply call.

    ``failure_kind is None`` means success; otherwise the file was
    rolled back (or never touched) and the entity should be
    surfaced in ``memory.dream.entity_failed`` telemetry.
    """

    entity_ref: str
    failure_kind: DreamApplyFailureKind | None
    error_message: str | None
    ops_applied: int


# Patch ops are allowed to touch these top-level keys only. Internal
# fields (`dream_processed_through`, `created_at`, `updated_at`, …)
# are managed by the runner; the LLM cannot overwrite them.
_ALLOWED_ROOTS: frozenset[str] = frozenset({
    "attributes",
    "relations",
    "aliases",
})

_ALLOWED_OPS: frozenset[str] = frozenset({"add", "replace", "remove"})


def apply_dream_output(
    *,
    workspace: Path,
    entity_ref: str,
    parsed: ParsedDreamOutput,
) -> DreamApplyResult:
    """Apply *parsed* to the entity page at
    ``workspace/memory/entities/<type>/<slug>.md``.

    Empty patch + empty body delta is a successful no-op (Rule 8).
    """
    type_, _, slug = entity_ref.partition(":")
    if not type_ or not slug:
        raise DreamApplyError(
            f"malformed entity_ref {entity_ref!r}: expected '<type>:<slug>'"
        )

    page_path = Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md"
    if not page_path.is_file():
        raise DreamApplyError(
            f"canonical page for {entity_ref!r} not found at {page_path}"
        )

    # Validation — fail fast, no disk writes.
    err = _validate_ops(parsed.patch_ops)
    if err is not None:
        return DreamApplyResult(
            entity_ref=entity_ref,
            failure_kind=DreamApplyFailureKind.VALIDATION,
            error_message=err,
            ops_applied=0,
        )

    # No-op short-circuit: empty patch + empty body delta = nothing to do.
    if not parsed.patch_ops and not parsed.body_delta:
        return DreamApplyResult(
            entity_ref=entity_ref,
            failure_kind=None,
            error_message=None,
            ops_applied=0,
        )

    # Copy current file to <path>.md.bak before any mutation.
    bak_path = page_path.with_suffix(".md.bak")
    try:
        bak_path.write_bytes(page_path.read_bytes())
    except OSError as exc:
        return DreamApplyResult(
            entity_ref=entity_ref,
            failure_kind=DreamApplyFailureKind.IO,
            error_message=f"failed to write .md.bak: {exc}",
            ops_applied=0,
        )

    try:
        page = EntityPage.from_file(page_path)
        if page is None:
            return _rollback(
                bak_path, page_path, entity_ref,
                DreamApplyFailureKind.ROUND_TRIP,
                "existing page failed to parse before apply",
                0,
            )

        ops_applied, patch_err = _apply_ops_to_page(page, parsed.patch_ops)
        if patch_err is not None:
            return _rollback(
                bak_path, page_path, entity_ref,
                DreamApplyFailureKind.PATCH_RUNTIME,
                patch_err,
                ops_applied,
            )

        # Append body delta (Rule 6).
        if parsed.body_delta:
            sep = "\n\n" if page.body and not page.body.endswith("\n") else "\n"
            page.body = (page.body + sep + parsed.body_delta).rstrip("\n")

        # Write atomically: render to text, then temp-and-rename.
        try:
            rendered = page.to_markdown()
        except EntityPageError as exc:
            return _rollback(
                bak_path, page_path, entity_ref,
                DreamApplyFailureKind.ROUND_TRIP,
                f"page failed to re-render: {exc}",
                ops_applied,
            )

        try:
            _atomic_write(page_path, rendered)
        except OSError as exc:
            return _rollback(
                bak_path, page_path, entity_ref,
                DreamApplyFailureKind.IO,
                f"atomic write failed: {exc}",
                ops_applied,
            )

        # Post-write round-trip check: re-parse the file we just wrote.
        reloaded = EntityPage.from_file(page_path)
        if reloaded is None:
            return _rollback(
                bak_path, page_path, entity_ref,
                DreamApplyFailureKind.ROUND_TRIP,
                "written page failed to re-parse",
                ops_applied,
            )

        # Success — drop the backup.
        try:
            bak_path.unlink()
        except OSError:  # pragma: no cover — best effort cleanup
            logger.warning("dream_apply: could not remove %s", bak_path)

        return DreamApplyResult(
            entity_ref=entity_ref,
            failure_kind=None,
            error_message=None,
            ops_applied=ops_applied,
        )
    except Exception as exc:  # noqa: BLE001
        # Catch-all so we never leave a half-written file.
        return _rollback(
            bak_path, page_path, entity_ref,
            DreamApplyFailureKind.IO,
            f"unexpected error: {exc}",
            0,
        )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _validate_ops(ops: list[dict[str, Any]]) -> str | None:
    """Return an error message if any op is malformed; None if all OK."""
    for i, op in enumerate(ops):
        op_kind = op.get("op")
        if op_kind not in _ALLOWED_OPS:
            return f"op #{i}: unknown op {op_kind!r}"
        path = op.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            return f"op #{i}: path must start with '/'"
        # First segment must be in _ALLOWED_ROOTS.
        parts = path.split("/")
        if len(parts) < 2 or parts[1] not in _ALLOWED_ROOTS:
            return (
                f"op #{i}: path {path!r} targets a forbidden root "
                f"(allowed: {sorted(_ALLOWED_ROOTS)})"
            )
        if op_kind in {"add", "replace"} and "value" not in op:
            return f"op #{i}: {op_kind} op missing 'value'"
        # Provenance required on every op (Rule 3).
        provenance = op.get("provenance")
        if not isinstance(provenance, str) or not provenance.strip():
            return f"op #{i}: missing or empty provenance"
    return None


def _apply_ops_to_page(
    page: EntityPage,
    ops: list[dict[str, Any]],
) -> tuple[int, str | None]:
    """Apply ops to ``page`` in-place. Returns (count, error_message).

    Wraps :mod:`jsonpatch` over a dict view of the patchable subset
    (`attributes`, `relations`, `aliases`). We don't hand the LLM ops
    to jsonpatch directly with the full frontmatter because that
    would let a malformed path escape into internal fields even
    though `_validate_ops` already gated them — defense in depth.
    """
    if not ops:
        return 0, None

    subset = {
        "attributes": dict(page.attributes),
        "relations": list(page.relations),
        "aliases": list(page.aliases),
    }
    # Strip the LLM's extra ``provenance`` key from each op before
    # handing to jsonpatch (RFC 6902 doesn't know about it).
    clean_ops: list[dict[str, Any]] = []
    for op in ops:
        clean = {k: v for k, v in op.items() if k != "provenance"}
        clean_ops.append(clean)

    try:
        patch = jsonpatch.JsonPatch(clean_ops)
        new_subset = patch.apply(subset)
    except (jsonpatch.JsonPatchException,
            jsonpointer.JsonPointerException) as exc:
        return 0, f"jsonpatch apply failed: {exc}"

    page.attributes = dict(new_subset.get("attributes", {}))
    page.relations = list(new_subset.get("relations", []))
    page.aliases = list(new_subset.get("aliases", []))

    # Record provenance per op into `page.provenance`. The shape
    # follows doc memory §3.5 — attribute provenance is keyed by
    # attribute name; relation provenance is keyed by index.
    prov = dict(page.provenance) if page.provenance else {}
    attr_prov = dict(prov.get("attributes") or {})
    rel_prov_list = list(prov.get("relations") or [])
    for op in ops:
        provenance = op["provenance"]
        path = op["path"]
        parts = path.split("/")
        # parts looks like ['', 'attributes', 'email'] or
        # ['', 'relations', '-'] / ['', 'relations', '3']
        if len(parts) < 3:
            continue
        root = parts[1]
        key = parts[2]
        if root == "attributes":
            attr_prov[key] = {"source_ref": provenance}
        elif root == "relations":
            if key == "-":
                idx = len(page.relations) - 1
            else:
                try:
                    idx = int(key)
                except ValueError:
                    continue
            rel_prov_list.append({"index": idx, "source_ref": provenance})
    if attr_prov:
        prov["attributes"] = attr_prov
    if rel_prov_list:
        prov["relations"] = rel_prov_list
    page.provenance = prov

    return len(ops), None


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* via temp-file + rename."""
    fd, tmp = tempfile.mkstemp(
        prefix=f"{path.name}.", dir=str(path.parent), text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _rollback(
    bak_path: Path,
    page_path: Path,
    entity_ref: str,
    kind: DreamApplyFailureKind,
    error_message: str,
    ops_applied: int,
) -> DreamApplyResult:
    """Restore from the .md.bak, drop the backup, and return a typed
    failure result."""
    try:
        if bak_path.exists():
            page_path.write_bytes(bak_path.read_bytes())
            bak_path.unlink()
    except OSError as exc:  # pragma: no cover — best effort
        logger.error(
            "dream_apply: rollback for %s failed: %s", entity_ref, exc,
        )
    return DreamApplyResult(
        entity_ref=entity_ref,
        failure_kind=kind,
        error_message=error_message,
        ops_applied=ops_applied,
    )
