"""The durin home data root.

A single env var, ``DURIN_HOME``, relocates durin's entire home data root
(``config.json``, ``secrets.json``, sessions, workspace, history, …) so a dev
(editable) install and a daily (pipx) install can keep separate state. Unset →
``~/.durin``, identical to before.

This is a dependency-free leaf module so ``loader``, ``paths`` and ``schema``
can all derive from it without import cycles.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_VAR = "DURIN_HOME"


def durin_home() -> Path:
    """Return the home data root: ``$DURIN_HOME`` if set, else ``~/.durin``."""
    raw = os.environ.get(_ENV_VAR)
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return Path.home() / ".durin"
