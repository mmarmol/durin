"""Atomic file writes: tempfile in the target's directory + fsync + os.replace.

A plain ``Path.write_text`` truncates the target first, so a crash mid-write
leaves a corrupt/partial file. Every durable write in durin (tool edits,
memory vault pages, plans, spills) should go through these helpers instead.

Pattern adapted from hermes-agent's memory store (MIT, Nous Research 2025):
- temp file created in the SAME directory as the target so ``os.replace`` is
  a same-filesystem rename (atomic), never a cross-device copy;
- ``fsync`` before the rename so the content is on disk when the name flips;
- symlinked targets are resolved first and updated IN PLACE — replacing the
  symlink with a regular file would detach dotfile-managed configs;
- the original file mode is preserved (``mkstemp`` creates 0o600); new files
  get 0o644.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def atomic_write_bytes(
    path: Path | str, data: bytes, *, fsync: bool = True, mode: int | None = None,
) -> Path:
    """Write ``data`` to ``path`` atomically. Returns the real (resolved) path.

    Raises ``OSError`` on failure; the original file (if any) is untouched
    and the temp file is removed.

    ``fsync=False`` skips the disk flush while keeping the tmp+rename
    atomicity (a crash can lose the latest content but never leaves a
    truncated file). Use it for derived/regenerable artifacts written on
    hot paths — e.g. the per-turn session .md mirror, which is rebuilt
    from history.jsonl (see SessionManager.save's deliberate no-fsync
    default).

    ``mode`` forces the final file permission (e.g. ``0o600`` for secrets) —
    ``mkstemp`` creates 0600 and a fresh file would otherwise land at 0644.
    When ``mode`` is None the existing file's mode is preserved, or 0644 for
    a new file.
    """
    target = Path(path)
    real = Path(os.path.realpath(target)) if target.is_symlink() else target
    real.parent.mkdir(parents=True, exist_ok=True)

    old_mode: int | None = None
    try:
        old_mode = stat.S_IMODE(real.stat().st_mode)
    except OSError:
        pass

    final_mode = mode if mode is not None else (
        old_mode if old_mode is not None else 0o644
    )

    fd, tmp_name = tempfile.mkstemp(
        dir=str(real.parent), prefix=f".{real.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.chmod(tmp, final_mode)
        os.replace(tmp, real)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return real


def atomic_write_text(
    path: Path | str, content: str, encoding: str = "utf-8", *,
    fsync: bool = True, mode: int | None = None,
) -> Path:
    """Text variant of :func:`atomic_write_bytes`."""
    return atomic_write_bytes(path, content.encode(encoding), fsync=fsync, mode=mode)
