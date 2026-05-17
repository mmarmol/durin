"""Persistent storage for plan state and event event log."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from durin.plan.types import PlanState


class PlanStore:
    """Writes plan.json and appends to events.jsonl for a session."""

    __slots__ = ("_path",)

    def __init__(self, workspace: Path, session_key: str) -> None:
        self._path = workspace / "plans" / session_key
        self._path.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def save_state(self, state: PlanState) -> None:
        plan_file = self._path / "plan.json"
        plan_file.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False))

    def append_event(self, event_type: str, **data: Any) -> None:
        entry = {"ts": time.time(), "type": event_type, **data}
        with open(self._path / "events.jsonl", "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def load_state(self) -> PlanState | None:
        plan_file = self._path / "plan.json"
        if not plan_file.exists():
            return None
        raw = json.loads(plan_file.read_text())
        from durin.plan.types import ExecutionTier, Phase, PlanItem
        items = [PlanItem(**it) for it in raw.get("items", [])]
        phase_val = raw.get("current_phase")
        return PlanState(
            goal=raw["goal"],
            tier=ExecutionTier(raw["tier"]),
            items=items,
            current_phase=Phase(phase_val) if phase_val else None,
            cycle_count=raw.get("cycle_count", 0),
        )
