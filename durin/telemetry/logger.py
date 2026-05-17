"""Append-only JSON-lines telemetry logger.

Writes one .jsonl file per session under ~/.cache/durin/telemetry/.
Each line is a self-contained event with timestamp, type, and payload.
Zero external dependencies — pure stdlib JSON + file append.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_DEFAULT_DIR = Path.home() / ".cache" / "durin" / "telemetry"
_MAX_EVENTS_PER_FILE = 10_000


class TelemetryLogger:
    """Append-only structured event logger for a single session."""

    __slots__ = ("_path", "_count")

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._count = 0

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        if self._count >= _MAX_EVENTS_PER_FILE:
            return
        entry = {
            "ts": time.time(),
            "type": event_type,
        }
        if data:
            entry["data"] = data
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
        self._count += 1

    def log_posture_initial(self, axes: dict[str, float]) -> None:
        self.log("posture.initial", {"axes": axes})

    def log_posture_change(
        self,
        axes: dict[str, float],
        deltas: dict[str, float],
        events: list[str],
    ) -> None:
        self.log("posture.change", {
            "axes": axes,
            "deltas": deltas,
            "stimulus_events": events,
        })

    def log_deliberation_start(
        self,
        trigger: str,
        goal_summary: str,
        posture_snapshot: dict[str, float],
    ) -> None:
        self.log("deliberation.start", {
            "trigger": trigger,
            "goal": goal_summary[:200],
            "posture": posture_snapshot,
        })

    def log_deliberation_result(
        self,
        winner_role: str,
        winner_score: float,
        threshold: float,
        rounds_used: int,
        under_doubt: bool,
        all_scores: list[dict[str, Any]],
        duration_ms: float,
    ) -> None:
        self.log("deliberation.result", {
            "winner": winner_role,
            "score": round(winner_score, 4),
            "threshold": round(threshold, 4),
            "rounds": rounds_used,
            "under_doubt": under_doubt,
            "all_scores": all_scores,
            "duration_ms": round(duration_ms, 1),
        })

    def log_deliberation_skipped(self, reason: str) -> None:
        self.log("deliberation.skipped", {"reason": reason})

    def log_deliberation_error(self, error: str) -> None:
        self.log("deliberation.error", {"error": error[:500]})

    def log_deliberation_v3(
        self,
        trigger: str,
        cycle: int,
        model: str,
        duration_ms: float,
        posture: dict[str, float],
        perspectives: dict[str, str],
        synthesis: str,
    ) -> None:
        self.log("deliberation.result", {
            "trigger": trigger,
            "cycle": cycle,
            "model": model,
            "duration_ms": round(duration_ms, 1),
            "posture": posture,
            "perspectives": perspectives,
            "synthesis": synthesis,
        })


def get_session_logger(
    session_key: str,
    base_dir: Path | None = None,
) -> TelemetryLogger:
    """Get or create a telemetry logger for a session.

    File naming: sanitized session key + date suffix for rotation.
    """
    import re
    from datetime import date

    target_dir = base_dir or _DEFAULT_DIR
    safe_key = re.sub(r"[^\w\-]", "_", session_key)[:80]
    safe_key = re.sub(r"\.{2,}", "_", safe_key)
    today = date.today().isoformat()
    filename = f"{safe_key}_{today}.jsonl"
    return TelemetryLogger(target_dir / filename)
