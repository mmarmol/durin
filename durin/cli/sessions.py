"""Session-discovery helpers shared between CLI entry points and the TUI.

A "session" lives at ``<workspace>/sessions/<safe_key>.jsonl`` where
``safe_key`` is ``f"{channel}:{chat_id}".replace(":", "_")``. The
``.meta.json`` sibling stores metadata; the ``.md`` sibling is the
rendered view.

Used by ``durin agent``'s session-pickup defaults and by the
``/sessions`` modal in the TUI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["SessionInfo", "list_sessions", "most_recent_session", "fresh_session_id"]


@dataclass(frozen=True)
class SessionInfo:
    channel: str
    chat_id: str
    path: Path
    mtime: float          # last modification (epoch)
    msg_count: int        # rough — jsonl line count

    @property
    def key(self) -> str:
        return f"{self.channel}:{self.chat_id}"

    @property
    def age_label(self) -> str:
        """Human-friendly 'last activity' label: e.g. '2h ago', '3d ago'."""
        now = datetime.now(tz=timezone.utc).timestamp()
        delta = max(0.0, now - self.mtime)
        if delta < 60:
            return f"{int(delta)}s ago"
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"


def list_sessions(workspace: Path) -> list[SessionInfo]:
    """Return all sessions in ``<workspace>/sessions/``, newest first."""
    sess_dir = workspace / "sessions"
    if not sess_dir.exists():
        return []
    out: list[SessionInfo] = []
    for path in sess_dir.glob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        stem = path.stem
        channel, _, chat_id = stem.partition("_")
        if not channel or not chat_id:
            continue
        msg_count = _count_jsonl_lines(path)
        out.append(
            SessionInfo(
                channel=channel,
                chat_id=chat_id,
                path=path,
                mtime=stat.st_mtime,
                msg_count=msg_count,
            )
        )
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def most_recent_session(workspace: Path) -> SessionInfo | None:
    """Return the most recently modified session, or ``None`` if there is none."""
    sessions = list_sessions(workspace)
    return sessions[0] if sessions else None


def fresh_session_id(channel: str = "cli") -> tuple[str, str]:
    """Return a (channel, chat_id) for a brand-new session, timestamp-based."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return channel, stamp


def _count_jsonl_lines(path: Path) -> int:
    """Cheap message count — counts lines in the .jsonl. Treats blank as none."""
    try:
        with path.open("rb") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0
