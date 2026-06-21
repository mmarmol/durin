"""TUI state persistence — lightweight JSON for cross-session UI data.

Currently stores ``recent_models`` (last 5 model names switched to via
the picker).  The file lives at ``~/.durin/tui-state.json`` and is
read on every picker open, written on every model switch.
"""

from __future__ import annotations

import json
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock

_MAX_RECENT = 5
_MAX_PROMPT_HISTORY = 50

_state_dir: Path | None = None


def _resolve_state_dir() -> Path:
    if _state_dir is not None:
        return _state_dir
    from durin.config.home import durin_home

    return durin_home()


def _state_file() -> Path:
    return _resolve_state_dir() / "tui-state.json"


def _load() -> dict:
    """Load the full state dict. Returns ``{}`` on missing/corrupt file."""
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — graceful degradation
        return {}


def _save(data: dict) -> None:
    """Write the state dict atomically, creating the parent dir if needed."""
    try:
        _state_file().parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(_state_file(), json.dumps(data, indent=2))
    except Exception:  # noqa: BLE001 — never crash the picker
        pass


def get_recent_models() -> list[str]:
    """Return the list of recently used model names (most recent first)."""
    data = _load()
    models = data.get("recent_models", [])
    if not isinstance(models, list):
        return []
    return [str(m) for m in models if isinstance(m, str)]


def add_recent_model(model: str) -> None:
    """Add *model* to the front of the recent list, dedup, cap at 5.

    The entire load→mutate→save is under cross_process_lock so concurrent
    processes cannot lose each other's writes.
    """
    if not model:
        return
    try:
        _state_file().parent.mkdir(parents=True, exist_ok=True)
        with cross_process_lock(_state_file()):
            data = _load()
            current = data.get("recent_models", [])
            if not isinstance(current, list):
                current = []
            models = [str(m) for m in current if isinstance(m, str)]
            models = [m for m in models if m != model]
            models.insert(0, model)
            data["recent_models"] = models[:_MAX_RECENT]
            _save(data)
    except Exception:  # noqa: BLE001 — never crash the picker
        pass


def get_prompt_history() -> list[str]:
    """Return the list of submitted prompts (most recent last)."""
    data = _load()
    history = data.get("prompt_history", [])
    if not isinstance(history, list):
        return []
    return [str(p) for p in history if isinstance(p, str)]


def add_prompt(text: str) -> None:
    """Append *text* to the prompt history, cap at 50 entries.

    The entire load→mutate→save is under cross_process_lock so concurrent
    processes cannot lose each other's writes.
    """
    text = text.strip()
    if not text:
        return
    try:
        _state_file().parent.mkdir(parents=True, exist_ok=True)
        with cross_process_lock(_state_file()):
            data = _load()
            history = data.get("prompt_history", [])
            if not isinstance(history, list):
                history = []
            history = [str(p) for p in history if isinstance(p, str)]
            history.append(text)
            data["prompt_history"] = history[-_MAX_PROMPT_HISTORY:]
            _save(data)
    except Exception:  # noqa: BLE001 — never crash the picker
        pass
