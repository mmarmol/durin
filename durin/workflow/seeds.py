"""Provenance-aware seeding of builtin workflow definitions.

Builtin workflow JSONs are copied from the wheel's templates into
``<workspace>/workflows/`` — files the user can then edit freely. That makes
upgrades ambiguous: overwriting clobbers customizations, never overwriting
freezes builtins at install day. This module resolves the ambiguity by
tracking provenance:

- ``.seeds.json`` records, per builtin name, the content hash the seeder last
  wrote. A file that still matches its recorded hash was never touched by the
  user — a newer template overwrites it automatically (committed to the
  workflows version store for rollback).
- A file that diverged from its recorded hash belongs to the user. A newer
  template becomes an entry in ``.seed_suggestions.json`` — surfaced in the
  UI and doctor — never a silent overwrite. Dismissing a suggestion
  tombstones that template version only; the next template version asks
  again.
- A workspace predating the manifest is adopted on first pass: files equal to
  the current template become tracked seeds; diverged ones are recorded with
  ``provenance: unknown`` (stale seed or user work — undecidable) and surface
  one suggestion for a human to settle. Applying it (or hand-editing the file
  to match the template) flips the entry to a tracked seed.

Skills need none of this: they are read live from the wheel and a workspace
copy shadows by name, so builtin skills are always current after an upgrade.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from durin.utils.atomic_write import atomic_write_text

MANIFEST_NAME = ".seeds.json"
SUGGESTIONS_NAME = ".seed_suggestions.json"
TOMBSTONES_NAME = ".seed_tombstones.json"
DIFF_CAP_LINES = 400


@dataclass
class SeedReport:
    installed: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    suggested: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def changed(self) -> list[str]:
        return self.installed + self.refreshed


def _workflows_dir(workspace: Path) -> Path:
    return Path(workspace) / "workflows"


def _default_templates_dir() -> Path | None:
    from importlib.resources import files as pkg_files

    try:
        tpl = Path(str(pkg_files("durin") / "templates" / "workflows"))
    except Exception:  # noqa: BLE001 - packaging quirks must not break startup
        return None
    return tpl if tpl.is_dir() else None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_json(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_templates(templates_dir: Path):
    for item in sorted(templates_dir.iterdir()):
        if item.name.endswith(".json") and not item.name.startswith("."):
            yield item.name[: -len(".json")], item.read_text(encoding="utf-8")


def _suggest(suggestions: dict, tombstones: dict, name: str, tpl_hash: str,
             reason: str, report: SeedReport) -> None:
    """Upsert a suggestion for this template version unless tombstoned or
    already pending for the same version."""
    if tombstones.get(f"{name}:{tpl_hash}"):
        return
    current = suggestions.get(name)
    if current and current.get("template_hash") == tpl_hash:
        return
    suggestions[name] = {
        "template_hash": tpl_hash, "reason": reason, "created_at": _now(),
    }
    report.suggested.append(name)


def refresh_seeds(workspace: Path, *, templates_dir: Path | None = None) -> SeedReport:
    """Reconcile workspace workflow seeds with the wheel's templates.

    Missing → install. Untouched-and-outdated → overwrite. Edited-and-outdated
    → suggestion. Never raises: seeding is best-effort startup work.
    """
    report = SeedReport()
    tpl_dir = templates_dir or _default_templates_dir()
    if tpl_dir is None:
        return report

    dest_dir = _workflows_dir(workspace)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        return report

    manifest = _read_json(dest_dir / MANIFEST_NAME)
    suggestions = _read_json(dest_dir / SUGGESTIONS_NAME)
    tombstones = _read_json(dest_dir / TOMBSTONES_NAME)

    for name, tpl_text in _iter_templates(tpl_dir):
        try:
            tpl_hash = _hash(tpl_text)
            dest = dest_dir / f"{name}.json"
            entry = manifest.get(name)

            if not dest.is_file():
                atomic_write_text(dest, tpl_text)
                manifest[name] = {"hash": tpl_hash, "provenance": "seed",
                                  "updated_at": _now()}
                suggestions.pop(name, None)
                report.installed.append(name)
                continue

            file_text = dest.read_text(encoding="utf-8")
            file_hash = _hash(file_text)

            if entry is None:
                # Pre-manifest workspace: adopt what matches, flag what doesn't.
                if file_hash == tpl_hash:
                    manifest[name] = {"hash": tpl_hash, "provenance": "seed",
                                      "updated_at": _now()}
                    report.adopted.append(name)
                else:
                    manifest[name] = {"hash": file_hash, "provenance": "unknown",
                                      "updated_at": _now()}
                    _suggest(suggestions, tombstones, name, tpl_hash,
                             "unknown-provenance", report)
                    report.unknown.append(name)
                continue

            if file_hash == tpl_hash:
                # In sync with the wheel (user may have hand-matched it).
                if entry.get("provenance") != "seed" or entry.get("hash") != tpl_hash:
                    manifest[name] = {"hash": tpl_hash, "provenance": "seed",
                                      "updated_at": _now()}
                suggestions.pop(name, None)
                continue

            if entry.get("provenance") == "seed" and file_hash == entry.get("hash"):
                # Untouched seed, template moved on → follow the wheel.
                atomic_write_text(dest, tpl_text)
                manifest[name] = {"hash": tpl_hash, "provenance": "seed",
                                  "updated_at": _now()}
                suggestions.pop(name, None)
                report.refreshed.append(name)
                continue

            # Diverged from what we seeded (or unknown provenance): the file is
            # the user's. Suggest only when the wheel actually has something new
            # relative to what they started from.
            if tpl_hash != entry.get("hash"):
                reason = ("edited" if entry.get("provenance") == "seed"
                          else "unknown-provenance")
                _suggest(suggestions, tombstones, name, tpl_hash, reason, report)
        except Exception:  # noqa: BLE001 - one bad template must not sink the rest
            logger.exception("seed refresh failed for workflow {}", name)

    try:
        _write_json(dest_dir / MANIFEST_NAME, manifest)
        _write_json(dest_dir / SUGGESTIONS_NAME, suggestions)
    except Exception:  # noqa: BLE001
        logger.exception("seed refresh could not persist state in {}", dest_dir)

    if report.changed:
        _commit(workspace, "seed refresh: " + ", ".join(report.changed))
    return report


def _commit(workspace: Path, reason: str) -> None:
    try:
        from durin.workflow.version_store import WorkflowVersionStore

        WorkflowVersionStore(_workflows_dir(workspace)).snapshot(reason)
    except Exception:  # noqa: BLE001 - versioning is best-effort
        logger.exception("seed refresh version snapshot failed")


def list_suggestions(workspace: Path, *, templates_dir: Path | None = None) -> list[dict]:
    """Pending seed-update suggestions, each with a unified diff (current file
    → new template), capped for transport."""
    dest_dir = _workflows_dir(workspace)
    suggestions = _read_json(dest_dir / SUGGESTIONS_NAME)
    tpl_dir = templates_dir or _default_templates_dir()
    out: list[dict] = []
    for name, entry in sorted(suggestions.items()):
        item = {"name": name, "reason": entry.get("reason", ""),
                "created_at": entry.get("created_at", ""), "diff": ""}
        if tpl_dir is not None:
            tpl_path = tpl_dir / f"{name}.json"
            dest = dest_dir / f"{name}.json"
            if tpl_path.is_file() and dest.is_file():
                diff = difflib.unified_diff(
                    dest.read_text(encoding="utf-8").splitlines(keepends=True),
                    tpl_path.read_text(encoding="utf-8").splitlines(keepends=True),
                    fromfile=f"{name}.json (yours)",
                    tofile=f"{name}.json (new builtin)",
                )
                lines = list(diff)
                if len(lines) > DIFF_CAP_LINES:
                    lines = lines[:DIFF_CAP_LINES] + ["... diff truncated ...\n"]
                item["diff"] = "".join(lines)
        out.append(item)
    return out


def apply_suggestion(workspace: Path, name: str, *,
                     templates_dir: Path | None = None) -> dict:
    """Overwrite the workspace file with the current template and track it as a
    seed again. The change is committed to the version store, so it is
    reviewable and revertible like any other edit."""
    dest_dir = _workflows_dir(workspace)
    tpl_dir = templates_dir or _default_templates_dir()
    tpl_path = (tpl_dir / f"{name}.json") if tpl_dir is not None else None
    if tpl_path is None or not tpl_path.is_file():
        return {"applied": False, "error": f"no builtin template named {name!r}"}

    tpl_text = tpl_path.read_text(encoding="utf-8")
    atomic_write_text(dest_dir / f"{name}.json", tpl_text)

    manifest = _read_json(dest_dir / MANIFEST_NAME)
    manifest[name] = {"hash": _hash(tpl_text), "provenance": "seed",
                      "updated_at": _now()}
    _write_json(dest_dir / MANIFEST_NAME, manifest)

    suggestions = _read_json(dest_dir / SUGGESTIONS_NAME)
    suggestions.pop(name, None)
    _write_json(dest_dir / SUGGESTIONS_NAME, suggestions)

    try:
        from durin.workflow.version_store import WorkflowVersionStore

        WorkflowVersionStore(dest_dir).commit_edit(
            name, "apply builtin seed update", actor="seed-refresh")
    except Exception:  # noqa: BLE001
        logger.exception("seed apply version commit failed for {}", name)
    return {"applied": True, "name": name}


def dismiss_suggestion(workspace: Path, name: str, *,
                       templates_dir: Path | None = None) -> dict:
    """Silence the pending suggestion for this template VERSION. A newer
    template version is a new decision and will surface again."""
    dest_dir = _workflows_dir(workspace)
    suggestions = _read_json(dest_dir / SUGGESTIONS_NAME)
    entry = suggestions.pop(name, None)
    if entry is None:
        return {"dismissed": False, "error": f"no pending suggestion for {name!r}"}
    _write_json(dest_dir / SUGGESTIONS_NAME, suggestions)

    tombstones = _read_json(dest_dir / TOMBSTONES_NAME)
    tombstones[f"{name}:{entry.get('template_hash', '')}"] = _now()
    _write_json(dest_dir / TOMBSTONES_NAME, tombstones)
    return {"dismissed": True, "name": name}
