"""Tool catalog: seed (bundled known_tools.json) + workspace override
(<workspace>/skills/.tool-catalog.json), merged at runtime."""
from __future__ import annotations

import json
from pathlib import Path

_SEED = Path(__file__).parent / "known_tools.json"


def load_catalog(workspace: Path | None = None) -> dict[str, dict]:
    """Merge the seed catalog with the workspace override (if any).

    Returns a dict mapping bin name → {primary: {kind, value}, alternatives: [...]}.
    Workspace entries override seed entries by key."""
    cat: dict[str, dict] = {}
    if _SEED.is_file():
        try:
            cat = json.loads(_SEED.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    if workspace is not None:
        ws_cat = Path(workspace) / "skills" / ".tool-catalog.json"
        if ws_cat.is_file():
            try:
                cat.update(json.loads(ws_cat.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                pass
    return cat
