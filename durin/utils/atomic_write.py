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

See docs/architecture/concurrency.md for the cross-process locking design that
wraps these helpers.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
from contextlib import suppress
from pathlib import Path


def atomic_write_bytes(
    path: Path | str, data: bytes, *, fsync: bool = True, fsync_dir: bool = False,
    mode: int | None = None,
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

    ``fsync_dir=True`` fsyncs the parent directory after ``os.replace`` so
    that the rename entry itself is durable on write-back filesystems.
    Silently skipped on platforms where opening a directory raises
    ``PermissionError`` (e.g. Windows, where NTFS journals metadata
    synchronously anyway).

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
        try:
            os.replace(tmp, real)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # Cross-device: tmp is on a different filesystem than the target
            # (e.g. /tmp vs /home on some Linux setups).  Fall back to a
            # same-dir temp so the final rename is still atomic.
            fd2, tmp2_name = tempfile.mkstemp(
                dir=str(real.parent), prefix=f".{real.name}.", suffix=".tmp"
            )
            tmp2 = Path(tmp2_name)
            try:
                with os.fdopen(fd2, "wb") as f2:
                    shutil.copyfileobj(tmp.open("rb"), f2)
                    f2.flush()
                    if fsync:
                        os.fsync(f2.fileno())
                os.chmod(tmp2, final_mode)
                os.replace(tmp2, real)
            except BaseException:
                with suppress(OSError):
                    tmp2.unlink()
                raise
            finally:
                with suppress(OSError):
                    tmp.unlink()
        if fsync_dir:
            with suppress(PermissionError):
                dfd = os.open(str(real.parent), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise
    return real


def atomic_write_text(
    path: Path | str, content: str, encoding: str = "utf-8", *,
    fsync: bool = True, fsync_dir: bool = False, mode: int | None = None,
) -> Path:
    """Text variant of :func:`atomic_write_bytes`."""
    return atomic_write_bytes(
        path, content.encode(encoding), fsync=fsync, fsync_dir=fsync_dir, mode=mode,
    )
