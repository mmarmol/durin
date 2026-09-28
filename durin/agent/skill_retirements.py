"""Skills someone retired, so they are not brought back without a person.

A retired skill was removed on purpose: it never worked, a better one replaced
it, or it was folded into another. Its record keeps the name, when, by whom,
why, and its replacement. The dream's authoring door refuses a retired name,
new gaps for it are routed to the replacement, and the skill extractor is told
the list. The in-session agent needs the user's explicit word to re-create one,
and an import of one goes to a person. Once the skill exists again, the record
is cleared.

Names are compared in one form (``name_key``): ``athena_logs`` and
``Athena Logs`` are the retired ``athena-logs``.

Records live in ``skills/.retired.jsonl``, inside the skills git store, so the
callers' own commits carry each change. A store whose removals predate the
records gets them from its history the first time they are read; the next
skills commit carries that file.
"""
from __future__ import annotations

import ast
import json
import re
from datetime import date
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text

_RETIRED = ".retired.jsonl"
# Commit subjects of the store operations that retire a skill.
_REMOVE_SUBJECT = re.compile(r"^skill\((?P<name>[^)]+)\): remove$")
_FUSE_SUBJECT = re.compile(r"^skill: fuse (?P<sources>\[.*?\]) -> (?P<target>\S+?): ")
_HISTORY_LIMIT = 100_000

OVERRIDE_RETIRED_HELP = (
    "Re-create a skill someone retired. ONLY when this call was refused as retired, "
    "you showed the user why it was retired and what replaces it, and the user "
    "explicitly said to bring it back — their word wins. Never set it on your own "
    "judgment."
)


def name_key(name: str) -> str:
    """One form for comparing skill names: ``athena_logs``, ``Athena Logs``
    and ``athena-logs`` name the same skill."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def _path(workspace: Path) -> Path:
    return Path(workspace) / "skills" / _RETIRED


def _store(workspace: Path):
    from durin.agent.skills_store import _store as skills_git_store
    return skills_git_store(Path(workspace))


def retired_skills(workspace: Path) -> dict[str, dict]:
    """Retired skills by name (the latest record for each)."""
    path = _path(workspace)
    if not path.exists():
        _backfill_from_history(workspace)
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


def retirement_for(workspace: Path, name: str) -> dict | None:
    """The retirement record for ``name`` in any spelling, or None."""
    key = name_key(name)
    return next((rec for retired, rec in retired_skills(workspace).items()
                 if name_key(retired) == key), None)


def record_retirement(workspace: Path, name: str, *, by: str, reason: str = "",
                      replaced_by: str | None = None) -> None:
    """Record that ``name`` was retired; the caller commits."""
    with _store(workspace).write_lock():
        records = retired_skills(workspace)
        records[name] = _record(name, date.today().isoformat(), by, reason, replaced_by)
        _write(workspace, records)


def clear_retirement(workspace: Path, name: str) -> bool:
    """Forget ``name``'s retirement (it exists again); the caller commits."""
    key = name_key(name)
    with _store(workspace).write_lock():
        records = retired_skills(workspace)
        kept = {n: r for n, r in records.items() if name_key(n) != key}
        if len(kept) == len(records):
            return False
        _write(workspace, kept)
        return True


def retirement_notice(rec: dict) -> str:
    """One sentence for a refusal: when, why, and what to use instead."""
    msg = f"skill `{rec.get('name')}` was retired on {rec.get('retired_at')}"
    if rec.get("reason"):
        msg += f" ({rec['reason']})"
    if rec.get("replaced_by"):
        msg += f"; extend `{rec['replaced_by']}` instead"
    return msg + "; it is not re-created automatically."


def retired_refusal(workspace: Path, name: str, *, can_override: bool) -> dict | None:
    """The refusal for authoring ``name`` when it is retired, or None.

    ``can_override``: the caller takes the user's word (``override_retired``),
    so the refusal says how to bring the skill back on it.
    """
    rec = retirement_for(workspace, name)
    if rec is None:
        return None
    out = {"error": retirement_notice(rec), "retired": True}
    if can_override:
        out["hint"] = ("Tell the user it was retired, why, and what replaces it. Only if "
                       "they explicitly ask to bring it back, retry with "
                       "override_retired=true.")
    return out


def _record(name: str, retired_at: str, by: str, reason: str,
            replaced_by: str | None) -> dict:
    return {
        "name": name,
        "retired_at": retired_at,
        "by": by,
        "reason": (reason or "").strip(),
        "replaced_by": replaced_by or None,
    }


def _backfill_from_history(workspace: Path) -> None:
    """Write the records of retirements the store made before these records
    existed: removals and fuse sources read from the skills history, oldest
    first so a later event wins, leaving out names that exist again. The file
    is written even when empty — it marks the history as read."""
    store = _store(workspace)
    if not store.is_initialized():
        return
    with store.write_lock():
        if _path(workspace).exists():
            return
        records: dict[str, dict] = {}
        for entry in reversed(store.log(max_entries=_HISTORY_LIMIT)):
            subject = entry.message.splitlines()[0] if entry.message else ""
            day = entry.timestamp[:10]
            removed = _REMOVE_SUBJECT.match(subject)
            if removed:
                name = removed["name"]
                records[name] = _record(name, day, "history", "", None)
                continue
            fused = _FUSE_SUBJECT.match(subject)
            if fused:
                try:
                    sources = ast.literal_eval(fused["sources"])
                except (ValueError, SyntaxError):
                    continue
                for source in sources if isinstance(sources, list) else []:
                    records[str(source)] = _record(
                        str(source), day, "history", f"fused into {fused['target']}",
                        fused["target"])
        skills_dir = _path(workspace).parent
        _write(workspace, {n: r for n, r in records.items()
                           if not (skills_dir / n / "SKILL.md").is_file()})


def _write(workspace: Path, records: dict[str, dict]) -> None:
    path = _path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                    for r in records.values()))
