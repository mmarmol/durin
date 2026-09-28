"""Skills someone retired, so the dream does not bring them back.

A retired skill was removed on purpose: it never worked, a better one replaced
it, or it was folded into another. Its record keeps the name, when, by whom,
why, and its replacement. The dream's authoring door refuses a retired name,
new gaps for it are routed to the replacement, and the skill extractor is told
the list. A person can still create the skill explicitly, which clears the
record.

Records live in ``skills/.retired.jsonl``, inside the skills git store, so the
callers' own commits carry each change.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text

_RETIRED = ".retired.jsonl"


def _path(workspace: Path) -> Path:
    return Path(workspace) / "skills" / _RETIRED


def retired_skills(workspace: Path) -> dict[str, dict]:
    """Retired skills by name (the latest record for each)."""
    path = _path(workspace)
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("name"):
            out[str(rec["name"])] = rec
    return out


def record_retirement(workspace: Path, name: str, *, by: str, reason: str = "",
                      replaced_by: str | None = None) -> None:
    """Record that ``name`` was retired; the caller commits."""
    records = retired_skills(workspace)
    records[name] = {
        "name": name,
        "retired_at": date.today().isoformat(),
        "by": by,
        "reason": (reason or "").strip(),
        "replaced_by": replaced_by or None,
    }
    _write(workspace, records)


def clear_retirement(workspace: Path, name: str) -> bool:
    """Forget ``name``'s retirement (it exists again); the caller commits."""
    records = retired_skills(workspace)
    if name not in records:
        return False
    del records[name]
    _write(workspace, records)
    return True


def retirement_notice(rec: dict) -> str:
    """One sentence for a refusal: when, why, and what to use instead."""
    msg = f"skill `{rec.get('name')}` was retired on {rec.get('retired_at')}"
    if rec.get("reason"):
        msg += f" ({rec['reason']})"
    if rec.get("replaced_by"):
        msg += f"; extend `{rec['replaced_by']}` instead"
    return msg + "; it is not re-created automatically."


def _write(workspace: Path, records: dict[str, dict]) -> None:
    path = _path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                    for r in records.values()))
