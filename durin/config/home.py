"""The durin home data root — the multi-instance boundary.

A durin **instance** is a self-contained data root selected by ``DURIN_HOME``.
Everything that is instance state lives relative to that root: ``config.json``
(incl. ports), ``secrets.json`` (incl. OAuth tokens via the secret store),
the memory store + vector index, sessions/history, workspace, and runtime data
(telemetry, logs, cron, media, webui threads). Unset → ``~/.durin``.

Point ``DURIN_HOME`` at a different dir and you get a fully independent instance
(its own config, ports, keys, memory). "Dev vs daily" is just two instances; a
test is a throwaway instance (see ``tests/conftest.py``). Two instances run
side-by-side — the gateway auto-picks a free port when the configured one is
taken (see ``durin/utils/net.py``).

The only things OUTSIDE the instance root are immutable, naturally-shared
artifacts that are NOT instance state: the embedding model-weights cache
(``~/.cache/huggingface``) and the durin package code / bundled webui.

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
